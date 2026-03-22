"""
Analyze trending content skill.

Analyzes what content is performing well in target topics right now by combining
Tavily AI search and Serper news results. Claude synthesizes the findings into
actionable content patterns and recommended angles. Use this before generating
content to ensure it rides current momentum rather than rehashing stale ideas.
"""

import asyncio
from typing import Any

import httpx
from anthropic import AsyncAnthropic
from tavily import TavilyClient

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.analyze_trending_content")

SERPER_NEWS_URL = "https://google.serper.dev/news"
SERPER_SEARCH_URL = "https://google.serper.dev/search"


class AnalyzeTrendingContentSkill(BaseTool):
    """Analyze what content is performing well in target topics right now."""

    name = "analyze_trending_content"
    description = (
        "Analyze trending content across topics. Returns top threads, "
        "content patterns, and recommended angles. Use before content "
        "generation to ride current momentum."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        self._tavily = TavilyClient(api_key=settings.tavily_api_key)
        self._anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def execute(
        self,
        topics: list[str],
        timeframe_hours: int = 48,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Analyze trending content for given topics.

        Args:
            topics: Topics to analyze (e.g. ["Web3 mentorship", "onchain"]).
            timeframe_hours: How far back to look (default 48h).

        Returns:
            Dict with top_threads, content_patterns, and recommended_angles.

        Raises:
            SkillExecutionError: If search or analysis fails.
        """
        try:
            # Run Tavily and Serper searches concurrently for all topics
            tavily_task = self._search_tavily(topics)
            serper_task = self._search_serper(topics, timeframe_hours)
            tavily_results, serper_results = await asyncio.gather(
                tavily_task, serper_task
            )

            # Merge all results into a single corpus for Claude to analyze
            all_results = tavily_results + serper_results
            if not all_results:
                logger.info("no_trending_results", topics=topics)
                return {
                    "top_threads": [],
                    "content_patterns": [],
                    "recommended_angles": [],
                }

            # Claude analyzes patterns and recommends angles
            analysis = await self._analyze_with_claude(all_results, topics)

            logger.info(
                "trending_content_analyzed",
                topics=topics,
                results_count=len(all_results),
                patterns_found=len(analysis.get("content_patterns", [])),
            )
            return analysis

        except SkillExecutionError:
            raise
        except Exception as exc:
            logger.error("analyze_trending_content_failed", error=str(exc))
            raise SkillExecutionError(
                f"Analyze trending content failed: {exc}"
            ) from exc

    async def _search_tavily(self, topics: list[str]) -> list[dict[str, Any]]:
        """Search Tavily for recent trending content on each topic.

        Args:
            topics: Topics to search.

        Returns:
            List of result dicts with title, url, content, source.
        """
        results: list[dict[str, Any]] = []
        for topic in topics:
            try:
                response = self._tavily.search(
                    query=f"trending {topic} content this week",
                    max_results=5,
                    search_depth="advanced",
                )
                for item in response.get("results", []):
                    results.append({
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "content": item.get("content", ""),
                        "source": "tavily",
                        "topic": topic,
                    })
            except Exception as exc:
                # Partial failure is fine — log and continue
                logger.warning("tavily_topic_search_failed", topic=topic, error=str(exc))

        return results

    async def _search_serper(
        self,
        topics: list[str],
        timeframe_hours: int,
    ) -> list[dict[str, Any]]:
        """Search Serper news and web for trending content.

        Args:
            topics: Topics to search.
            timeframe_hours: How far back to look.

        Returns:
            List of result dicts with title, url, content, source.
        """
        results: list[dict[str, Any]] = []
        # Map timeframe to Serper's tbs parameter — approximate
        if timeframe_hours <= 24:
            tbs = "qdr:d"
        elif timeframe_hours <= 72:
            tbs = "qdr:d3"
        else:
            tbs = "qdr:w"

        headers = {
            "X-API-KEY": settings.serper_api_key,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            for topic in topics:
                # Search both news and regular web for broader coverage
                for url in [SERPER_NEWS_URL, SERPER_SEARCH_URL]:
                    try:
                        response = await client.post(
                            url,
                            headers=headers,
                            json={"q": f"{topic} trending", "num": 5, "tbs": tbs},
                        )
                        response.raise_for_status()
                        data = response.json()

                        # News results come under "news", web under "organic"
                        items = data.get("news", data.get("organic", []))
                        for item in items:
                            results.append({
                                "title": item.get("title", ""),
                                "url": item.get("link", ""),
                                "content": item.get("snippet", ""),
                                "source": "serper",
                                "topic": topic,
                            })
                    except Exception as exc:
                        logger.warning(
                            "serper_topic_search_failed",
                            topic=topic,
                            url=url,
                            error=str(exc),
                        )

        return results

    async def _analyze_with_claude(
        self,
        results: list[dict[str, Any]],
        topics: list[str],
    ) -> dict[str, Any]:
        """Use Claude to identify patterns and recommend content angles.

        Args:
            results: Combined search results from Tavily and Serper.
            topics: Original topics for context.

        Returns:
            Dict with top_threads, content_patterns, recommended_angles.
        """
        topics_str = ", ".join(topics)

        # Build a concise digest for Claude — truncate long content
        digest_lines = []
        for r in results[:30]:  # Cap at 30 to stay within context limits
            snippet = r["content"][:300] if r["content"] else ""
            digest_lines.append(
                f"- [{r['source']}] {r['title']}\n  URL: {r['url']}\n  {snippet}"
            )
        digest = "\n".join(digest_lines)

        response = await self._anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Analyze these trending results for topics: {topics_str}\n\n"
                        f"{digest}\n\n"
                        "Return a JSON object with exactly these keys:\n"
                        "1. top_threads — array of the 5 most interesting/viral pieces. "
                        "Each has: title, url, why_its_trending (1 sentence).\n"
                        "2. content_patterns — array of 3-5 string observations about "
                        "what content formats/angles are working right now.\n"
                        "3. recommended_angles — array of 3-5 specific content ideas "
                        "we should create, each a string. Be specific and non-obvious.\n\n"
                        "Return ONLY valid JSON, no markdown fences, no explanation."
                    ),
                }
            ],
        )

        raw_text = response.content[0].text.strip()

        # Parse Claude's JSON response
        import json

        try:
            # Strip markdown fences if Claude adds them despite instructions
            if raw_text.startswith("```"):
                raw_text = raw_text.split("\n", 1)[1]
                if raw_text.endswith("```"):
                    raw_text = raw_text[: raw_text.rfind("```")]
            analysis = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning("claude_json_parse_failed", raw_text=raw_text[:200])
            # Fallback — return raw results as top threads
            analysis = {
                "top_threads": [
                    {"title": r["title"], "url": r["url"], "why_its_trending": ""}
                    for r in results[:5]
                ],
                "content_patterns": ["Could not parse patterns — review raw results"],
                "recommended_angles": ["Could not parse angles — review raw results"],
            }

        return analysis

    def get_schema(self) -> dict:
        """Return JSON schema for analyze_trending_content inputs."""
        return {
            "type": "object",
            "properties": {
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Topics to analyze trending content for",
                },
                "timeframe_hours": {
                    "type": "integer",
                    "default": 48,
                    "description": "How far back to look in hours",
                },
            },
            "required": ["topics"],
        }
