"""
Generate hashtag strategy skill.

Researches and recommends hashtags for content based on current volume and
relevance. Enforces platform-specific limits (X: 2-3 max, LinkedIn: 3-5 fine)
and filters out generic/spam hashtags. Uses Tavily to research current hashtag
usage and Claude to synthesize a final strategy.
"""

from typing import Any

from anthropic import AsyncAnthropic
from tavily import TavilyClient

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.generate_hashtag_strategy")

# Platform limits — more than these and engagement actually drops
_PLATFORM_LIMITS = {
    "x": 3,
    "linkedin": 5,
}

# Generic hashtags that signal lazy marketing — never recommend these
_BANNED_HASHTAGS = {
    "#motivation", "#success", "#entrepreneur", "#hustle", "#grind",
    "#blessed", "#mindset", "#goals", "#inspiration", "#business",
    "#followme", "#follow4follow", "#like4like", "#instagood",
    "#love", "#happy", "#photooftheday", "#beautiful", "#tbt",
}


class GenerateHashtagStrategySkill(BaseTool):
    """Research and recommend hashtags for content."""

    name = "generate_hashtag_strategy"
    description = (
        "Generate a hashtag strategy for content. Returns primary, secondary, "
        "and avoid lists with platform-specific limits. Researches current "
        "volume — never generic hashtags."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        self._tavily = TavilyClient(api_key=settings.tavily_api_key)
        self._anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def execute(
        self,
        content: str,
        topics: list[str],
        platform: str = "x",
        **kwargs: Any,
    ) -> dict[str, list[str]]:
        """Generate a hashtag strategy for the given content and platform.

        Args:
            content: The draft content to generate hashtags for.
            topics: Related topics for broader hashtag research.
            platform: Target platform — 'x' or 'linkedin'.

        Returns:
            Dict with primary (use these), secondary (rotate these), avoid (skip these).

        Raises:
            SkillExecutionError: If research or analysis fails.
        """
        if platform not in _PLATFORM_LIMITS:
            raise SkillExecutionError(
                f"Unsupported platform '{platform}'. Use 'x' or 'linkedin'."
            )

        try:
            # Step 1: Research current hashtag landscape for these topics
            research = await self._research_hashtags(topics, platform)

            # Step 2: Have Claude synthesize a strategy
            strategy = await self._generate_strategy(
                content, topics, platform, research
            )

            logger.info(
                "hashtag_strategy_generated",
                platform=platform,
                primary_count=len(strategy.get("primary", [])),
                secondary_count=len(strategy.get("secondary", [])),
            )
            return strategy

        except SkillExecutionError:
            raise
        except Exception as exc:
            logger.error(
                "generate_hashtag_strategy_failed",
                platform=platform,
                error=str(exc),
            )
            raise SkillExecutionError(
                f"Generate hashtag strategy failed: {exc}"
            ) from exc

    async def _research_hashtags(
        self,
        topics: list[str],
        platform: str,
    ) -> str:
        """Research current hashtag usage for the given topics.

        Args:
            topics: Topics to research hashtags for.
            platform: Platform context (X vs LinkedIn have different cultures).

        Returns:
            Combined research text for Claude to analyze.
        """
        platform_name = "Twitter/X" if platform == "x" else "LinkedIn"
        research_pieces: list[str] = []

        for topic in topics:
            try:
                results = self._tavily.search(
                    query=f"best {platform_name} hashtags for {topic} 2026",
                    max_results=3,
                    search_depth="basic",
                )
                for item in results.get("results", []):
                    research_pieces.append(
                        f"[{topic}] {item.get('title', '')}: "
                        f"{item.get('content', '')[:300]}"
                    )
            except Exception as exc:
                logger.warning(
                    "hashtag_research_failed",
                    topic=topic,
                    error=str(exc),
                )

        return "\n".join(research_pieces) if research_pieces else "No research data available."

    async def _generate_strategy(
        self,
        content: str,
        topics: list[str],
        platform: str,
        research: str,
    ) -> dict[str, list[str]]:
        """Use Claude to generate the final hashtag strategy.

        Args:
            content: The draft content.
            topics: Related topics.
            platform: Target platform.
            research: Raw research data from Tavily.

        Returns:
            Dict with primary, secondary, avoid lists.
        """
        max_primary = _PLATFORM_LIMITS[platform]
        platform_name = "Twitter/X" if platform == "x" else "LinkedIn"
        topics_str = ", ".join(topics)

        banned_str = ", ".join(sorted(_BANNED_HASHTAGS)[:10])

        response = await self._anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Generate a hashtag strategy for this {platform_name} post.\n\n"
                        f"CONTENT:\n{content[:500]}\n\n"
                        f"TOPICS: {topics_str}\n\n"
                        f"RESEARCH ON CURRENT HASHTAG TRENDS:\n{research[:1500]}\n\n"
                        f"RULES:\n"
                        f"- Platform: {platform_name}\n"
                        f"- Max primary hashtags: {max_primary}\n"
                        f"- NEVER use generic hashtags like {banned_str}\n"
                        f"- Hashtags must be specific and niche-relevant\n"
                        f"- Include the # prefix\n\n"
                        f"Return ONLY valid JSON with these keys:\n"
                        f'- "primary": array of {max_primary} best hashtags to use\n'
                        f'- "secondary": array of 3-5 alternates to rotate in future posts\n'
                        f'- "avoid": array of 3-5 hashtags to avoid for this content '
                        f"(too generic, wrong audience, saturated)\n\n"
                        f"No markdown fences, no explanation — just the JSON."
                    ),
                }
            ],
        )

        raw_text = response.content[0].text.strip()

        import json

        try:
            if raw_text.startswith("```"):
                raw_text = raw_text.split("\n", 1)[1]
                if raw_text.endswith("```"):
                    raw_text = raw_text[: raw_text.rfind("```")]
            strategy = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning("hashtag_json_parse_failed", raw_text=raw_text[:200])
            strategy = {
                "primary": [],
                "secondary": [],
                "avoid": list(_BANNED_HASHTAGS)[:5],
            }

        # Post-process: enforce limits and filter banned hashtags
        strategy["primary"] = self._filter_hashtags(
            strategy.get("primary", []), max_primary
        )
        strategy["secondary"] = self._filter_hashtags(
            strategy.get("secondary", []), 5
        )
        # Ensure avoid list always includes generics that Claude might have missed
        avoid = set(strategy.get("avoid", []))
        avoid.update(h for h in _BANNED_HASHTAGS if h.lower() in {
            t.lower() for t in topics
        } or len(avoid) < 3)
        strategy["avoid"] = list(avoid)[:5]

        return strategy

    @staticmethod
    def _filter_hashtags(hashtags: list[str], limit: int) -> list[str]:
        """Filter out banned hashtags and enforce the limit.

        Args:
            hashtags: Raw hashtag list.
            limit: Maximum number to keep.

        Returns:
            Cleaned, limited list of hashtags.
        """
        cleaned = []
        for tag in hashtags:
            tag = tag.strip()
            if not tag.startswith("#"):
                tag = f"#{tag}"
            if tag.lower() not in {b.lower() for b in _BANNED_HASHTAGS}:
                cleaned.append(tag)
        return cleaned[:limit]

    def get_schema(self) -> dict:
        """Return JSON schema for generate_hashtag_strategy inputs."""
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The draft content to generate hashtags for",
                },
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Related topics for hashtag research",
                },
                "platform": {
                    "type": "string",
                    "enum": ["x", "linkedin"],
                    "default": "x",
                    "description": "Target platform",
                },
            },
            "required": ["content", "topics"],
        }
