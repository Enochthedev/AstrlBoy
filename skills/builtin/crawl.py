"""
Firecrawl site crawler skill.

Crawls an entire site starting from a URL, following links up to a configurable
depth. Returns all pages as markdown. Use for deep competitor analysis.
"""

from typing import Any

from firecrawl import FirecrawlApp

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.crawl")


class CrawlSkill(BaseTool):
    """Crawl a website and return all pages as markdown."""

    name = "crawl"
    description = "Crawl a website starting from a URL. Returns all discovered pages as markdown."
    version = "1.0.0"

    def __init__(self) -> None:
        self._client = FirecrawlApp(api_key=settings.firecrawl_api_key)

    async def execute(
        self, url: str, max_pages: int = 10, max_depth: int = 2, **kwargs: Any
    ) -> list[dict[str, str]]:
        """Crawl a website and return page contents.

        Args:
            url: The starting URL.
            max_pages: Maximum number of pages to crawl.
            max_depth: Maximum link depth to follow.

        Returns:
            List of dicts with 'url' and 'markdown' keys.

        Raises:
            SkillExecutionError: If the crawl fails.
        """
        try:
            # Firecrawl v2 SDK renamed crawl_url → crawl
            crawl_fn = getattr(self._client, "crawl", None) or self._client.crawl_url
            result = crawl_fn(
                url,
                params={
                    "limit": max_pages,
                    "maxDepth": max_depth,
                    "scrapeOptions": {"formats": ["markdown"]},
                },
            )
            pages = []
            for page in result.get("data", []):
                pages.append({
                    "url": page.get("metadata", {}).get("sourceURL", url),
                    "markdown": page.get("markdown", ""),
                })
            return pages
        except Exception as exc:
            logger.error("crawl_failed", url=url, error=str(exc))
            raise SkillExecutionError(f"Crawl failed for {url}: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for crawl inputs."""
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Starting URL to crawl"},
                "max_pages": {"type": "integer", "description": "Max pages to crawl", "default": 10},
                "max_depth": {"type": "integer", "description": "Max link depth", "default": 2},
            },
            "required": ["url"],
        }
