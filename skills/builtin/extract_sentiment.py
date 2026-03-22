"""
Sentiment extraction skill.

Searches user-generated content about a product or topic across Reddit, Twitter,
Product Hunt, and other sources, then uses Claude to classify overall sentiment,
extract pain points, praise points, and feature requests. Feeds into the
intelligence and feedback graphs.
"""

import json
from typing import Any

from anthropic import AsyncAnthropic
from tavily import TavilyClient

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.extract_sentiment")

# Claude client for sentiment analysis
_anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)

# Source name → Tavily include_domains mapping
_SOURCE_DOMAINS: dict[str, list[str]] = {
    "reddit": ["reddit.com"],
    "twitter": ["twitter.com", "x.com"],
    "producthunt": ["producthunt.com"],
    "hackernews": ["news.ycombinator.com"],
    "linkedin": ["linkedin.com"],
}


class ExtractSentimentSkill(BaseTool):
    """Extract sentiment and pain points from user-generated content.

    Combines Tavily search (scoped to specific platforms) with Claude analysis
    to produce a structured sentiment report. Use this to understand how users
    feel about a product, competitor, or topic before generating content or
    compiling briefings.
    """

    name = "extract_sentiment"
    description = (
        "Extract sentiment, pain points, and feature requests from user-generated "
        "content about a product or topic. Searches Reddit, Twitter, Product Hunt, etc."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        self._tavily = TavilyClient(api_key=settings.tavily_api_key)

    async def execute(
        self,
        target: str,
        sources: list[str] | None = None,
        timeframe_days: int = 7,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Search for and analyze sentiment about a target.

        Args:
            target: Product name, topic, or company to analyze sentiment for.
            sources: Platforms to search. Defaults to ['reddit', 'twitter', 'producthunt'].
                Valid values: reddit, twitter, producthunt, hackernews, linkedin.
            timeframe_days: How many days back to search. Defaults to 7.

        Returns:
            Dict with overall_sentiment, pain_points, praise_points,
            feature_requests, and raw_samples.

        Raises:
            SkillExecutionError: If search or analysis fails.
        """
        if sources is None:
            sources = ["reddit", "twitter", "producthunt"]

        # --- 1. Search each source via Tavily ---
        all_results: list[dict[str, Any]] = []
        for source in sources:
            domains = _SOURCE_DOMAINS.get(source)
            if not domains:
                logger.warning(
                    "unknown_sentiment_source",
                    source=source,
                    target=target,
                )
                continue

            try:
                response = self._tavily.search(
                    query=f"{target} reviews opinions feedback",
                    max_results=5,
                    search_depth="advanced",
                    include_domains=domains,
                    days=timeframe_days,
                )
                results = response.get("results", [])
                # Tag each result with its source for downstream analysis
                for r in results:
                    r["_source"] = source
                all_results.extend(results)
            except Exception as exc:
                # Log and continue — partial results are better than none
                logger.warning(
                    "sentiment_search_failed",
                    source=source,
                    target=target,
                    error=str(exc),
                )

        if not all_results:
            logger.warning(
                "no_sentiment_results",
                target=target,
                sources=sources,
            )
            return {
                "overall_sentiment": "neutral",
                "pain_points": [],
                "praise_points": [],
                "feature_requests": [],
                "raw_samples": [],
            }

        # --- 2. Use Claude to analyze sentiment ---
        try:
            analysis = await self._analyze_sentiment(target, all_results)
        except Exception as exc:
            logger.error(
                "sentiment_analysis_failed",
                target=target,
                error=str(exc),
            )
            raise SkillExecutionError(
                f"Sentiment analysis failed for '{target}': {exc}"
            ) from exc

        logger.info(
            "sentiment_extracted",
            target=target,
            sources=sources,
            overall_sentiment=analysis.get("overall_sentiment", "unknown"),
            pain_point_count=len(analysis.get("pain_points", [])),
            sample_count=len(analysis.get("raw_samples", [])),
        )

        return analysis

    async def _analyze_sentiment(
        self,
        target: str,
        search_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Use Claude to classify sentiment and extract structured insights.

        Args:
            target: The product/topic being analyzed.
            search_results: Raw Tavily search results with _source tags.

        Returns:
            Structured sentiment analysis dict.
        """
        # Build a condensed representation of search results for Claude
        samples_text = ""
        for i, result in enumerate(search_results[:15], 1):
            source = result.get("_source", "unknown")
            title = result.get("title", "")
            content = result.get("content", "")[:500]
            samples_text += f"\n[{i}] Source: {source}\nTitle: {title}\nContent: {content}\n"

        prompt = (
            f"You are a sentiment analysis expert. Analyze the following user-generated "
            f"content about '{target}' and extract structured insights.\n\n"
            f"CONTENT:\n{samples_text}\n\n"
            f"Respond with JSON only (no markdown fences):\n"
            f'{{\n'
            f'  "overall_sentiment": "positive" | "neutral" | "negative" | "mixed",\n'
            f'  "pain_points": ["specific pain point 1", "..."],\n'
            f'  "praise_points": ["specific praise point 1", "..."],\n'
            f'  "feature_requests": ["feature request 1", "..."],\n'
            f'  "raw_samples": [\n'
            f'    {{"source": "reddit", "text": "relevant quote", "sentiment": "positive|neutral|negative"}}\n'
            f'  ]\n'
            f'}}\n\n'
            f"Rules:\n"
            f"- Overall sentiment reflects the aggregate tone across all samples\n"
            f"- Pain points are specific problems users mention, not vague complaints\n"
            f"- Feature requests are things users explicitly wish existed\n"
            f"- Include 3-5 raw_samples that best represent the range of sentiment\n"
            f"- Be precise and evidence-based — only report what the content actually says"
        )

        response = await _anthropic.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = response.content[0].text.strip()

        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning(
                "sentiment_parse_failed",
                target=target,
                raw_text=raw_text[:500],
            )
            return {
                "overall_sentiment": "neutral",
                "pain_points": [],
                "praise_points": [],
                "feature_requests": [],
                "raw_samples": [],
            }

    def get_schema(self) -> dict:
        """Return JSON schema for extract_sentiment inputs."""
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Product name, topic, or company to analyze",
                },
                "sources": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["reddit", "twitter", "producthunt", "hackernews", "linkedin"],
                    },
                    "default": ["reddit", "twitter", "producthunt"],
                    "description": "Platforms to search for user-generated content",
                },
                "timeframe_days": {
                    "type": "integer",
                    "default": 7,
                    "description": "How many days back to search",
                },
            },
            "required": ["target"],
        }
