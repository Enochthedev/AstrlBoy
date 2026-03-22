"""
Deep topic research skill.

Performs surface or deep research on a topic using Tavily search and optionally
Firecrawl for full-page content extraction on top results. Claude synthesizes
everything into a structured research brief with key points, sources, and
content angles. Output is stored to R2 for future reference and training.
"""

import json
from typing import Any
from uuid import uuid4

from anthropic import AsyncAnthropic
from firecrawl import FirecrawlApp
from tavily import TavilyClient

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool
from storage.r2 import r2_client

logger = get_logger("skills.research_topic")

# Claude client for research synthesis
_anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)


class ResearchTopicSkill(BaseTool):
    """Deep research on a topic to inform content or briefings.

    Surface mode uses Tavily basic search only — fast, good for trend checks.
    Deep mode adds Tavily advanced search plus Firecrawl scraping on the top 3
    results for full-page content, giving Claude richer material to synthesize.
    All research is stored to R2 for training data and audit trails.
    """

    name = "research_topic"
    description = (
        "Research a topic at surface or deep level. Returns a synthesis with "
        "key points, sources, and content angles. Stores output to R2."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        self._tavily = TavilyClient(api_key=settings.tavily_api_key)
        self._firecrawl = FirecrawlApp(api_key=settings.firecrawl_api_key)

    async def execute(
        self,
        topic: str,
        depth: str = "surface",
        focus: str | None = None,
        contract_slug: str = "astrlboy",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Research a topic and return a structured synthesis.

        Args:
            topic: The topic to research.
            depth: 'surface' for Tavily basic only, 'deep' for advanced search
                plus Firecrawl on top results.
            focus: Optional angle to narrow the research (e.g. 'pricing models',
                'user adoption challenges').
            contract_slug: Client slug for R2 storage. Defaults to 'astrlboy'.

        Returns:
            Dict with summary, key_points, sources, content_angles, and r2_key.

        Raises:
            SkillExecutionError: If search or synthesis fails.
        """
        if depth not in ("surface", "deep"):
            raise SkillExecutionError(
                f"Invalid depth '{depth}' — must be 'surface' or 'deep'"
            )

        query = f"{topic} {focus}" if focus else topic

        # --- 1. Tavily search ---
        search_depth = "basic" if depth == "surface" else "advanced"
        try:
            response = self._tavily.search(
                query=query,
                max_results=10 if depth == "deep" else 5,
                search_depth=search_depth,
            )
            search_results = response.get("results", [])
        except Exception as exc:
            logger.error(
                "research_search_failed",
                topic=topic,
                depth=depth,
                error=str(exc),
            )
            raise SkillExecutionError(
                f"Research search failed for '{topic}': {exc}"
            ) from exc

        if not search_results:
            raise SkillExecutionError(
                f"No search results found for '{topic}'"
            )

        # --- 2. Deep mode: Firecrawl top 3 results for full page content ---
        full_page_content: list[dict[str, Any]] = []
        if depth == "deep":
            top_urls = [r["url"] for r in search_results[:3] if r.get("url")]
            for url in top_urls:
                try:
                    scrape_result = self._firecrawl.scrape_url(
                        url,
                        params={"formats": ["markdown"]},
                    )
                    markdown = scrape_result.get("markdown", "")
                    if markdown:
                        full_page_content.append({
                            "url": url,
                            "markdown": markdown[:6000],
                        })
                except Exception as exc:
                    # Non-fatal — we still have the Tavily summaries
                    logger.warning(
                        "research_scrape_failed",
                        url=url,
                        topic=topic,
                        error=str(exc),
                    )

        # --- 3. Claude synthesis ---
        try:
            synthesis = await self._synthesize(
                topic=topic,
                focus=focus,
                search_results=search_results,
                full_page_content=full_page_content,
            )
        except Exception as exc:
            logger.error(
                "research_synthesis_failed",
                topic=topic,
                error=str(exc),
            )
            raise SkillExecutionError(
                f"Research synthesis failed for '{topic}': {exc}"
            ) from exc

        # --- 4. Store to R2 ---
        research_id = uuid4()
        try:
            r2_key = await r2_client.dump(
                contract_slug=contract_slug,
                entity_type="research",
                entity_id=research_id,
                data={
                    "topic": topic,
                    "depth": depth,
                    "focus": focus,
                    "search_results": [
                        {"url": r.get("url"), "title": r.get("title")}
                        for r in search_results
                    ],
                    "synthesis": synthesis,
                    "model": "claude-sonnet-4-6",
                },
            )
        except Exception as exc:
            # Non-fatal — return synthesis even if R2 fails
            logger.warning(
                "research_r2_failed",
                topic=topic,
                error=str(exc),
            )
            r2_key = ""

        # Build source list with relevance scores from Tavily
        sources = [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "relevance_score": r.get("score", 0.0),
            }
            for r in search_results
        ]

        logger.info(
            "topic_researched",
            topic=topic,
            depth=depth,
            focus=focus,
            source_count=len(sources),
            key_point_count=len(synthesis.get("key_points", [])),
        )

        return {
            "summary": synthesis.get("summary", ""),
            "key_points": synthesis.get("key_points", []),
            "sources": sources,
            "content_angles": synthesis.get("content_angles", []),
            "r2_key": r2_key,
        }

    async def _synthesize(
        self,
        topic: str,
        focus: str | None,
        search_results: list[dict[str, Any]],
        full_page_content: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Use Claude to synthesize search results into a structured brief.

        Args:
            topic: The topic being researched.
            focus: Optional angle for the research.
            search_results: Tavily search results.
            full_page_content: Full-page markdown from Firecrawl (deep mode only).

        Returns:
            Dict with summary, key_points, and content_angles.
        """
        # Build context from search results
        search_context = ""
        for i, result in enumerate(search_results, 1):
            title = result.get("title", "")
            content = result.get("content", "")[:400]
            url = result.get("url", "")
            search_context += f"\n[{i}] {title}\nURL: {url}\n{content}\n"

        # Add full-page content if available (deep mode)
        deep_context = ""
        if full_page_content:
            deep_context = "\n\nFULL PAGE CONTENT (top sources):\n"
            for page in full_page_content:
                deep_context += f"\n--- {page['url']} ---\n{page['markdown'][:4000]}\n"

        focus_instruction = ""
        if focus:
            focus_instruction = f"\nFocus angle: {focus}\n"

        prompt = (
            f"You are a research analyst. Synthesize the following search results "
            f"about '{topic}' into a structured research brief.\n"
            f"{focus_instruction}\n"
            f"SEARCH RESULTS:\n{search_context}"
            f"{deep_context}\n\n"
            f"Respond with JSON only (no markdown fences):\n"
            f'{{\n'
            f'  "summary": "2-3 paragraph synthesis of what you found",\n'
            f'  "key_points": [\n'
            f'    "Key finding 1",\n'
            f'    "Key finding 2"\n'
            f'  ],\n'
            f'  "content_angles": [\n'
            f'    "Angle 1: a specific content piece we could create from this research",\n'
            f'    "Angle 2: another angle"\n'
            f'  ]\n'
            f'}}\n\n'
            f"Rules:\n"
            f"- Summary should be informative and dense — no filler\n"
            f"- Key points are concrete, factual findings (5-8 points)\n"
            f"- Content angles are specific ideas for content we could produce\n"
            f"- Include 3-5 content angles, each with a clear hook"
        )

        response = await _anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = response.content[0].text.strip()

        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning(
                "research_synthesis_parse_failed",
                topic=topic,
                raw_text=raw_text[:500],
            )
            return {
                "summary": raw_text[:1000],
                "key_points": [],
                "content_angles": [],
            }

    def get_schema(self) -> dict:
        """Return JSON schema for research_topic inputs."""
        return {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The topic to research",
                },
                "depth": {
                    "type": "string",
                    "enum": ["surface", "deep"],
                    "default": "surface",
                    "description": "surface = Tavily basic only; deep = advanced search + Firecrawl top 3",
                },
                "focus": {
                    "type": "string",
                    "description": "Optional angle to narrow the research",
                },
                "contract_slug": {
                    "type": "string",
                    "default": "astrlboy",
                    "description": "Client slug for R2 storage",
                },
            },
            "required": ["topic"],
        }
