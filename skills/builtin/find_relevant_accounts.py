"""
Find relevant X accounts skill.

Discovers X accounts worth following or engaging with based on topic relevance.
Combines Tavily web search to find account names with X API user lookup for
profile data, then uses Claude to score relevance. Prioritizes mid-tier accounts
(1k-100k followers) where engagement is most impactful.
"""

import asyncio
from typing import Any

import tweepy
from anthropic import AsyncAnthropic
from tavily import TavilyClient

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.find_relevant_accounts")

# Mid-tier accounts yield the best engagement ROI — big enough to matter,
# small enough to notice you.
_MID_TIER_LOW = 1_000
_MID_TIER_HIGH = 100_000


class FindRelevantAccountsSkill(BaseTool):
    """Find X accounts worth following/engaging with based on topic relevance."""

    name = "find_relevant_accounts"
    description = (
        "Find X accounts relevant to given topics. Returns scored profiles "
        "with bio, follower count, and relevance score. Best for discovering "
        "engagement targets and potential collaborators."
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
        min_followers: int = 500,
        max_followers: int = 500_000,
        limit: int = 20,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Find X accounts relevant to the given topics.

        Args:
            topics: Topics to search for (e.g. ["mentorship", "Web3"]).
            min_followers: Minimum follower count to include.
            max_followers: Maximum follower count to include.
            limit: Maximum number of accounts to return.

        Returns:
            List of dicts with user_id, username, bio, followers, relevance_score.

        Raises:
            SkillExecutionError: If search or lookup fails.
        """
        try:
            # Step 1: Search Tavily for relevant account names across all topics
            usernames = await self._search_for_usernames(topics)
            if not usernames:
                logger.info("no_usernames_found", topics=topics)
                return []

            # Step 2: Look up profiles via X API
            profiles = self._lookup_profiles(usernames)
            if not profiles:
                logger.info("no_profiles_found", usernames_searched=len(usernames))
                return []

            # Step 3: Filter by follower count
            filtered = [
                p for p in profiles
                if min_followers <= p["followers"] <= max_followers
            ]
            if not filtered:
                logger.info(
                    "all_profiles_filtered_by_followers",
                    total=len(profiles),
                    min_followers=min_followers,
                    max_followers=max_followers,
                )
                return []

            # Step 4: Score relevance with Claude
            scored = await self._score_relevance(filtered, topics)

            # Step 5: Sort by relevance, prioritize mid-tier, return top N
            scored.sort(key=lambda x: x["relevance_score"], reverse=True)
            results = scored[:limit]

            logger.info(
                "accounts_found",
                topics=topics,
                searched=len(usernames),
                returned=len(results),
            )
            return results

        except SkillExecutionError:
            raise
        except Exception as exc:
            logger.error("find_relevant_accounts_failed", error=str(exc))
            raise SkillExecutionError(
                f"Find relevant accounts failed: {exc}"
            ) from exc

    async def _search_for_usernames(self, topics: list[str]) -> list[str]:
        """Search Tavily for X account names related to each topic.

        Args:
            topics: List of topics to search.

        Returns:
            Deduplicated list of X usernames found.
        """
        queries = [
            f"top {topic} accounts on Twitter X" for topic in topics
        ] + [
            f"influential {topic} Twitter accounts to follow" for topic in topics
        ]

        usernames: set[str] = set()
        for query in queries:
            results = self._tavily.search(
                query=query,
                max_results=5,
                search_depth="basic",
            )
            for result in results.get("results", []):
                # Extract @usernames from search result content
                content = result.get("content", "") + " " + result.get("title", "")
                extracted = self._extract_usernames(content)
                usernames.update(extracted)

        return list(usernames)

    @staticmethod
    def _extract_usernames(text: str) -> list[str]:
        """Extract @usernames from text.

        Args:
            text: Raw text that may contain @mentions.

        Returns:
            List of clean usernames (without the @ prefix).
        """
        import re

        # Match @username patterns — alphanumeric and underscores, 1-15 chars
        matches = re.findall(r"@([A-Za-z0-9_]{1,15})", text)
        # Filter out common false positives
        stopwords = {"gmail", "yahoo", "hotmail", "outlook", "email", "com", "twitter"}
        return [m for m in matches if m.lower() not in stopwords]

    def _lookup_profiles(self, usernames: list[str]) -> list[dict[str, Any]]:
        """Look up X profiles for a list of usernames.

        X API allows max 100 usernames per request, so we batch accordingly.

        Args:
            usernames: List of X usernames to look up.

        Returns:
            List of profile dicts with user_id, username, bio, followers.
        """
        profiles: list[dict[str, Any]] = []
        # X API get_users_by_usernames supports max 100 per call
        batch_size = 100

        for i in range(0, len(usernames), batch_size):
            batch = usernames[i : i + batch_size]
            try:
                response = self._x_client.get_users_by_usernames(
                    usernames=batch,
                    user_fields=["description", "public_metrics", "verified"],
                )
                if response and response.data:
                    for user in response.data:
                        metrics = user.public_metrics or {}
                        profiles.append({
                            "user_id": str(user.id),
                            "username": user.username,
                            "bio": user.description or "",
                            "followers": metrics.get("followers_count", 0),
                            "verified": getattr(user, "verified", False),
                        })
            except Exception as exc:
                # Log but continue — partial results are better than none
                logger.warning(
                    "x_user_lookup_partial_failure",
                    batch_start=i,
                    error=str(exc),
                )

        return profiles

    async def _score_relevance(
        self,
        profiles: list[dict[str, Any]],
        topics: list[str],
    ) -> list[dict[str, Any]]:
        """Use Claude to score each profile's relevance to the given topics.

        Args:
            profiles: List of profile dicts to score.
            topics: Topics to score relevance against.

        Returns:
            Same profiles with 'relevance_score' added (0.0-1.0).
        """
        topics_str = ", ".join(topics)
        profiles_block = "\n".join(
            f"- @{p['username']} ({p['followers']} followers): {p['bio'][:200]}"
            for p in profiles
        )

        response = await self._anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Score each X account's relevance to these topics: {topics_str}\n\n"
                        f"Accounts:\n{profiles_block}\n\n"
                        "For each account, respond with exactly one line in this format:\n"
                        "@username|score\n\n"
                        "Score is a float from 0.0 to 1.0 where 1.0 = perfectly relevant.\n"
                        "Give a bonus to mid-tier accounts (1k-100k followers) since they "
                        "engage more.\n"
                        "Only output the lines, nothing else."
                    ),
                }
            ],
        )

        # Parse scores from Claude's response
        score_map: dict[str, float] = {}
        raw_text = response.content[0].text.strip()
        for line in raw_text.split("\n"):
            line = line.strip()
            if "|" in line:
                parts = line.split("|")
                username = parts[0].replace("@", "").strip()
                try:
                    score = float(parts[1].strip())
                    score_map[username] = max(0.0, min(1.0, score))
                except ValueError:
                    continue

        # Apply mid-tier bonus and merge scores back into profiles
        for profile in profiles:
            base_score = score_map.get(profile["username"], 0.5)
            # Boost mid-tier accounts — they're the sweet spot for engagement
            if _MID_TIER_LOW <= profile["followers"] <= _MID_TIER_HIGH:
                base_score = min(1.0, base_score + 0.05)
            profile["relevance_score"] = round(base_score, 3)

        return profiles

    def get_schema(self) -> dict:
        """Return JSON schema for find_relevant_accounts inputs."""
        return {
            "type": "object",
            "properties": {
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Topics to find relevant accounts for",
                },
                "min_followers": {
                    "type": "integer",
                    "default": 500,
                    "description": "Minimum follower count",
                },
                "max_followers": {
                    "type": "integer",
                    "default": 500000,
                    "description": "Maximum follower count",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Max accounts to return",
                },
            },
            "required": ["topics"],
        }
