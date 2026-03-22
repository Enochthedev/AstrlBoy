"""
Tavily AI search skill.

Performs AI-powered web searches that return relevant, summarized results.
Use for trend research, finding relevant threads, and general web intelligence.
"""

from typing import Any

from tavily import TavilyClient

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.search")


class SearchSkill(BaseTool):
    """AI-powered web search via Tavily."""

    name = "search"
    description = "AI-powered web search. Returns relevant, summarized results for any query."
    version = "1.0.0"

    def __init__(self) -> None:
        self._client = TavilyClient(api_key=settings.tavily_api_key)

    async def execute(
        self,
        query: str,
        max_results: int = 5,
        search_depth: str = "basic",
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Search the web for a query.

        Args:
            query: The search query.
            max_results: Maximum number of results to return.
            search_depth: 'basic' or 'advanced' — advanced is slower but more thorough.

        Returns:
            List of result dicts with 'title', 'url', 'content' keys.

        Raises:
            SkillExecutionError: If the search fails.
        """
        try:
            response = self._client.search(
                query=query,
                max_results=max_results,
                search_depth=search_depth,
            )
            return response.get("results", [])
        except Exception as exc:
            logger.error("search_failed", query=query, error=str(exc))
            raise SkillExecutionError(f"Search failed for '{query}': {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for search inputs."""
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "default": 5},
                "search_depth": {
                    "type": "string",
                    "enum": ["basic", "advanced"],
                    "default": "basic",
                },
            },
            "required": ["query"],
        }
