"""
Find engagement opportunities skill.

Discovers threads and conversations worth joining right now. Combines Tavily
search for fresh discussions with X API tweet search for real-time activity.
Claude scores each opportunity and suggests a non-obvious engagement angle.
Quality over quantity — only surfaces threads where engagement would be
genuinely valuable, not spammy.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import tweepy
from anthropic import AsyncAnthropic
from tavily import TavilyClient

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.find_engagement_opportunities")


class FindEngagementOpportunitiesSkill(BaseTool):
    """Find threads and conversations worth joining right now."""

    name = "find_engagement_opportunities"
    description = (
        "Find high-value engagement opportunities — threads, conversations, "
        "and posts worth replying to. Returns scored opportunities with "
        "suggested engagement angles. Quality over quantity."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        self._tavily = TavilyClient(api_key=settings.tavily_api_key)
        self._x_client = tweepy.Client(
            bearer_token=settings.twitter_bearer_token,
            consumer_key=settings.twitter_api_key,
            consumer_secret=settings.twitter_api_secret,
            access_token=settings.twitter_access_token,
            access_token_secret=settings.twitter_access_secret,
        )
        self._anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def execute(
        self,
        topics: list[str],
        platforms: list[str] | None = None,
        min_engagement: int = 5,
        max_age_hours: int = 12,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Find engagement opportunities across platforms.

        Args:
            topics: Topics to find conversations about.
            platforms: Platforms to search (default ["x"]).
            min_engagement: Minimum engagement count (likes+replies) to qualify.
            max_age_hours: Maximum age of content in hours.

        Returns:
            List of opportunity dicts with platform, url, summary,
            engagement_count, why_relevant, suggested_angle.

        Raises:
            SkillExecutionError: If search or scoring fails.
        """
        if platforms is None:
            platforms = ["x"]

        try:
            # Gather raw candidates from all requested platforms concurrently
            tasks = []
            if "x" in platforms:
                tasks.append(
                    self._search_x(topics, min_engagement, max_age_hours)
                )
            # Tavily covers web-wide discussions (forums, blogs, etc.)
            tasks.append(self._search_tavily(topics, max_age_hours))

            all_candidates: list[dict[str, Any]] = []
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.warning(
                        "platform_search_partial_failure",
                        error=str(result),
                    )
                else:
                    all_candidates.extend(result)

            if not all_candidates:
                logger.info("no_engagement_opportunities", topics=topics)
                return []

            # Deduplicate by URL
            seen_urls: set[str] = set()
            unique_candidates: list[dict[str, Any]] = []
            for c in all_candidates:
                url = c.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    unique_candidates.append(c)

            # Score and suggest angles with Claude
            scored = await self._score_and_suggest(unique_candidates, topics)

            # Sort by relevance score descending, return top results
            scored.sort(
                key=lambda x: x.get("relevance_score", 0), reverse=True
            )

            # Only return genuinely good opportunities (score >= 6/10)
            filtered = [s for s in scored if s.get("relevance_score", 0) >= 6]

            logger.info(
                "engagement_opportunities_found",
                topics=topics,
                candidates=len(unique_candidates),
                qualified=len(filtered),
            )
            return filtered

        except SkillExecutionError:
            raise
        except Exception as exc:
            logger.error("find_engagement_opportunities_failed", error=str(exc))
            raise SkillExecutionError(
                f"Find engagement opportunities failed: {exc}"
            ) from exc

    async def _search_x(
        self,
        topics: list[str],
        min_engagement: int,
        max_age_hours: int,
    ) -> list[dict[str, Any]]:
        """Search X for recent tweets with engagement on the given topics.

        Args:
            topics: Topics to search for.
            min_engagement: Minimum engagement threshold.
            max_age_hours: How far back to look.

        Returns:
            List of candidate dicts.
        """
        candidates: list[dict[str, Any]] = []
        start_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

        for topic in topics:
            try:
                # Build query: topic + minimum retweets/likes to filter noise
                # -is:retweet excludes pure RTs, lang:en keeps it parseable
                query = f"{topic} -is:retweet lang:en"

                response = self._x_client.search_recent_tweets(
                    query=query,
                    max_results=10,
                    start_time=start_time,
                    tweet_fields=[
                        "created_at",
                        "public_metrics",
                        "conversation_id",
                        "text",
                    ],
                    expansions=["author_id"],
                    user_fields=["username", "public_metrics"],
                )

                if not response or not response.data:
                    continue

                # Build author lookup
                authors: dict[str, str] = {}
                if response.includes and "users" in response.includes:
                    for user in response.includes["users"]:
                        authors[str(user.id)] = user.username

                for tweet in response.data:
                    metrics = tweet.public_metrics or {}
                    engagement = (
                        metrics.get("like_count", 0)
                        + metrics.get("reply_count", 0)
                        + metrics.get("retweet_count", 0)
                    )

                    if engagement < min_engagement:
                        continue

                    author = authors.get(str(tweet.author_id), "unknown")
                    candidates.append({
                        "platform": "x",
                        "url": f"https://x.com/{author}/status/{tweet.id}",
                        "summary": tweet.text[:280],
                        "engagement_count": engagement,
                        "author": author,
                        "created_at": str(tweet.created_at) if tweet.created_at else None,
                        "topic": topic,
                    })

            except Exception as exc:
                logger.warning(
                    "x_search_topic_failed", topic=topic, error=str(exc)
                )

        return candidates

    async def _search_tavily(
        self,
        topics: list[str],
        max_age_hours: int,
    ) -> list[dict[str, Any]]:
        """Search Tavily for recent discussions and threads.

        Args:
            topics: Topics to search.
            max_age_hours: How far back to look.

        Returns:
            List of candidate dicts.
        """
        candidates: list[dict[str, Any]] = []

        for topic in topics:
            try:
                results = self._tavily.search(
                    query=f"{topic} discussion thread conversation",
                    max_results=5,
                    search_depth="basic",
                )
                for item in results.get("results", []):
                    url = item.get("url", "")
                    # Determine platform from URL
                    platform = "web"
                    if "twitter.com" in url or "x.com" in url:
                        platform = "x"
                    elif "linkedin.com" in url:
                        platform = "linkedin"
                    elif "reddit.com" in url:
                        platform = "reddit"

                    candidates.append({
                        "platform": platform,
                        "url": url,
                        "summary": item.get("content", "")[:300],
                        "engagement_count": 0,  # Tavily doesn't provide this
                        "author": "",
                        "created_at": None,
                        "topic": topic,
                    })
            except Exception as exc:
                logger.warning(
                    "tavily_engagement_search_failed",
                    topic=topic,
                    error=str(exc),
                )

        return candidates

    async def _score_and_suggest(
        self,
        candidates: list[dict[str, Any]],
        topics: list[str],
    ) -> list[dict[str, Any]]:
        """Use Claude to score opportunities and suggest engagement angles.

        The angle should be non-obvious — not just "great point!" but something
        that adds genuine value to the conversation.

        Args:
            candidates: Raw candidate opportunities.
            topics: Original topics for context.

        Returns:
            Candidates enriched with relevance_score, why_relevant, suggested_angle.
        """
        if not candidates:
            return []

        topics_str = ", ".join(topics)

        # Build digest — limit to 25 candidates to stay within context limits
        capped = candidates[:25]
        digest_lines = []
        for i, c in enumerate(capped):
            digest_lines.append(
                f"{i}. [{c['platform']}] {c['summary'][:200]}\n"
                f"   URL: {c['url']} | Engagement: {c['engagement_count']}"
            )
        digest = "\n".join(digest_lines)

        response = await self._anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Score these engagement opportunities for topics: {topics_str}\n\n"
                        f"We are an AI agent (@astrlboy_) that provides sharp, "
                        f"knowledgeable commentary. We want to engage where we can "
                        f"add genuine value — not generic replies.\n\n"
                        f"CANDIDATES:\n{digest}\n\n"
                        f"For each candidate (by index), respond with one line:\n"
                        f"index|score|why_relevant|suggested_angle\n\n"
                        f"- score: integer 1-10 (10 = must engage)\n"
                        f"- why_relevant: 1 sentence on why this matters\n"
                        f"- suggested_angle: specific, non-obvious reply angle. "
                        f"NOT 'great point' or 'I agree'. Something that adds value.\n\n"
                        f"Only output the lines, nothing else."
                    ),
                }
            ],
        )

        raw_text = response.content[0].text.strip()

        # Parse Claude's response and merge into candidates
        score_data: dict[int, dict[str, Any]] = {}
        for line in raw_text.split("\n"):
            line = line.strip()
            if not line or "|" not in line:
                continue
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            try:
                idx = int(parts[0].strip())
                score = int(parts[1].strip())
                score_data[idx] = {
                    "relevance_score": max(1, min(10, score)),
                    "why_relevant": parts[2].strip(),
                    "suggested_angle": parts[3].strip(),
                }
            except (ValueError, IndexError):
                continue

        # Merge scores back into candidates
        results: list[dict[str, Any]] = []
        for i, candidate in enumerate(capped):
            enrichment = score_data.get(i, {
                "relevance_score": 5,
                "why_relevant": "Could not score — review manually",
                "suggested_angle": "Review the thread and draft a response",
            })
            results.append({
                "platform": candidate["platform"],
                "url": candidate["url"],
                "summary": candidate["summary"],
                "engagement_count": candidate["engagement_count"],
                "why_relevant": enrichment["why_relevant"],
                "suggested_angle": enrichment["suggested_angle"],
                "relevance_score": enrichment["relevance_score"],
            })

        return results

    def get_schema(self) -> dict:
        """Return JSON schema for find_engagement_opportunities inputs."""
        return {
            "type": "object",
            "properties": {
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Topics to find engagement opportunities for",
                },
                "platforms": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["x"],
                    },
                    "default": ["x"],
                    "description": "Platforms to search",
                },
                "min_engagement": {
                    "type": "integer",
                    "default": 5,
                    "description": "Minimum engagement count (likes+replies+RTs)",
                },
                "max_age_hours": {
                    "type": "integer",
                    "default": 12,
                    "description": "Maximum age of content in hours",
                },
            },
            "required": ["topics"],
        }
