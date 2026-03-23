"""
Firecrawl URL scraper skill.

Scrapes a single URL and returns clean markdown. Handles JS-rendered pages.
Use for competitor pages, articles, job postings.
For crawling entire sites, use the crawl skill instead.
"""

from typing import Any

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.scrape")


class ScrapeSkill(BaseTool):
    """Scrape a URL and return clean markdown."""

    name = "scrape"
    description = "Scrape a URL and return clean markdown. Handles JS-rendered pages. Use for competitor pages, articles, job postings."
    version = "1.1.0"

    def __init__(self) -> None:
        # Firecrawl v4+ exports Firecrawl class; older versions export FirecrawlApp
        try:
            from firecrawl import Firecrawl
            self._client = Firecrawl(api_key=settings.firecrawl_api_key)
            self._version = "v2"
        except ImportError:
            from firecrawl import FirecrawlApp
            self._client = FirecrawlApp(api_key=settings.firecrawl_api_key)
            self._version = "v1"

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
            if self._version == "v2":
                # Firecrawl v4+: scrape(url, formats=[...])
                formats = ["markdown"]
                if extract_schema:
                    formats.append("json")

                result = self._client.scrape(url, formats=formats)

                if extract_schema and hasattr(result, "get"):
                    return result.get("json", result.get("markdown", ""))
                if hasattr(result, "markdown"):
                    return result.markdown or ""
                if hasattr(result, "get"):
                    return result.get("markdown", "")
                return str(result)
            else:
                # Firecrawl v1: scrape_url(url, params={...})
                params: dict[str, Any] = {"formats": ["markdown"]}
                if extract_schema:
                    params["formats"].append("extract")
                    params["extract"] = {"schema": extract_schema}

                result = self._client.scrape_url(url, params=params)

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
