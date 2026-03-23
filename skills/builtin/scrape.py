"""
Firecrawl URL scraper skill.

Scrapes a single URL and returns clean markdown. Use for competitor pages,
articles, job postings. For crawling entire sites, use the crawl skill instead.
"""

from typing import Any

from firecrawl import FirecrawlApp

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.scrape")


class ScrapeSkill(BaseTool):
    """Scrape a URL and return clean markdown."""

    name = "scrape"
    description = "Scrape a URL and return clean markdown. Use for competitor pages, articles, job postings."
    version = "1.0.0"

    def __init__(self) -> None:
        self._client = FirecrawlApp(api_key=settings.firecrawl_api_key)

    async def execute(self, url: str, extract_schema: dict | None = None, **kwargs: Any) -> str:
        """Scrape a URL and return markdown content.

        Args:
            url: The URL to scrape.
            extract_schema: Optional JSON schema for structured data extraction.

        Returns:
            Markdown content from the page.

        Raises:
            SkillExecutionError: If the scrape fails.
        """
        try:
            params: dict[str, Any] = {"formats": ["markdown"]}
            if extract_schema:
                params["formats"].append("extract")
                params["extract"] = {"schema": extract_schema}

            # Firecrawl v2 SDK renamed scrape_url → scrape
            scrape_fn = getattr(self._client, "scrape", None) or self._client.scrape_url
            result = scrape_fn(url, params=params)

            if extract_schema and result.get("extract"):
                return result["extract"]
            return result.get("markdown", "")
        except Exception as exc:
            logger.error("scrape_failed", url=url, error=str(exc))
            raise SkillExecutionError(f"Scrape failed for {url}: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for scrape inputs."""
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to scrape"},
                "extract_schema": {
                    "type": "object",
                    "description": "Optional JSON schema for structured extraction",
                },
            },
            "required": ["url"],
        }
