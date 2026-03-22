"""
Lightweight URL fetch skill.

Fetches a URL using httpx and returns the raw text content. Use this for
simple static pages where you just need the text — no JavaScript rendering,
no Firecrawl credits burned. For anything that needs JS rendering or clean
markdown extraction, use the scrape skill instead.
"""

import asyncio
from typing import Any

import httpx

from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.fetch_page")

# Retry config — 3 attempts with exponential backoff (1s, 2s, 4s)
_MAX_RETRIES = 3
_BASE_DELAY = 1.0


class FetchPageSkill(BaseTool):
    """Fetch a URL and return raw text content, status, and headers.

    This is the cheapest way to read a page. No external API keys needed,
    just a plain HTTP GET. Use scrape for JS-rendered pages or when you
    need clean markdown output.
    """

    name = "fetch_page"
    description = (
        "Lightweight fetch of a URL — returns raw text, status code, final URL, "
        "and headers. Use scrape for JS-rendered pages."
    )
    version = "1.0.0"

    async def execute(
        self,
        url: str,
        follow_redirects: bool = True,
        timeout: int = 10,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Fetch a URL and return its text content.

        Args:
            url: The URL to fetch.
            follow_redirects: Whether to follow HTTP redirects. Defaults to True.
            timeout: Request timeout in seconds. Defaults to 10.

        Returns:
            Dict with 'status_code', 'text', 'final_url', and 'headers'.

        Raises:
            SkillExecutionError: If the fetch fails after all retries.
        """
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(
                    follow_redirects=follow_redirects,
                    timeout=httpx.Timeout(timeout),
                ) as client:
                    response = await client.get(url)

                result = {
                    "status_code": response.status_code,
                    "text": response.text,
                    "final_url": str(response.url),
                    "headers": dict(response.headers),
                }

                logger.info(
                    "page_fetched",
                    url=url,
                    status_code=response.status_code,
                    final_url=result["final_url"],
                    content_length=len(response.text),
                )
                return result

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "fetch_page_retry",
                    url=url,
                    attempt=attempt,
                    max_retries=_MAX_RETRIES,
                    error=str(exc),
                )
                if attempt < _MAX_RETRIES:
                    # Exponential backoff: 1s, 2s, 4s
                    await asyncio.sleep(_BASE_DELAY * (2 ** (attempt - 1)))

        # All retries exhausted
        logger.error("fetch_page_failed", url=url, error=str(last_exc))
        raise SkillExecutionError(
            f"Fetch failed for {url} after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    def get_schema(self) -> dict:
        """Return JSON schema for fetch_page inputs."""
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "follow_redirects": {
                    "type": "boolean",
                    "description": "Follow HTTP redirects (default true)",
                    "default": True,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Request timeout in seconds (default 10)",
                    "default": 10,
                },
            },
            "required": ["url"],
        }
