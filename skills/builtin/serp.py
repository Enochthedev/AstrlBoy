"""
Serper Google SERP data skill.

Returns raw Google search results including organic results, knowledge graph,
people also ask, etc. Use when you need structured SERP data rather than
AI-summarized content (use the search skill for that).
"""

from typing import Any

import httpx

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.serp")

SERPER_API_URL = "https://google.serper.dev/search"


class SerpSkill(BaseTool):
    """Google SERP data via Serper API."""

    name = "serp"
    description = "Get raw Google SERP data — organic results, knowledge graph, people also ask."
    version = "1.0.0"

    async def execute(
        self,
        query: str,
        num_results: int = 10,
        gl: str = "us",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Fetch Google SERP data for a query.

        Args:
            query: The search query.
            num_results: Number of results to return.
            gl: Country code for localized results.

        Returns:
            Dict containing organic results, knowledge graph, PAA, etc.

        Raises:
            SkillExecutionError: If the API call fails.
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    SERPER_API_URL,
                    headers={
                        "X-API-KEY": settings.serper_api_key,
                        "Content-Type": "application/json",
                    },
                    json={"q": query, "num": num_results, "gl": gl},
                )
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            logger.error("serp_failed", query=query, error=str(exc))
            raise SkillExecutionError(f"SERP failed for '{query}': {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for serp inputs."""
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "num_results": {"type": "integer", "default": 10},
                "gl": {"type": "string", "default": "us", "description": "Country code"},
            },
            "required": ["query"],
        }
