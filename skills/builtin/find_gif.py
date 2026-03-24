"""
GIF search skill using Tenor.

Searches for relevant GIFs and returns URLs that can be attached to X posts.
Use when a post would land better with a visual reaction — celebrations, memes,
reactions, emphasis. The agent picks the right GIF based on description; you
decide whether to include it in the post.

Requires TENOR_API_KEY — a Google Cloud API key with the Tenor API enabled.
Free tier: 300 requests/minute, no cost.
"""

from typing import Any

import httpx

from core.config import settings
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.find_gif")

_TENOR_BASE = "https://tenor.googleapis.com/v2"


class FindGifSkill(BaseTool):
    """Search Tenor for a GIF matching a description.

    Returns a ranked list of GIF URLs with titles. Use the URL with post_x's
    media_url parameter to attach the GIF to a tweet.
    """

    name = "find_gif"
    description = (
        "Search Tenor for a GIF or meme reaction to attach to a post. "
        "Describe what you want (e.g. 'mind blown', 'awkward silence', 'this is fine fire', "
        "'waiting skeleton', 'stonks') and get back matching GIF URLs. "
        "Use when a visual would add humor, emphasis, or reaction to a tweet. "
        "Returns preview URLs — pass the chosen URL to post_x as media_url."
    )
    version = "1.0.0"

    async def execute(
        self,
        query: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Search Tenor for GIFs matching the query.

        Args:
            query: Description of the GIF you want (e.g. 'mind blown', 'stonks meme').
            limit: Number of results to return (max 10).

        Returns:
            Dict with 'gifs' list (each has 'url', 'preview_url', 'title').
        """
        if not settings.tenor_api_key:
            return {
                "status": "unavailable",
                "reason": "TENOR_API_KEY not configured",
            }

        limit = min(limit, 10)

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_TENOR_BASE}/search",
                params={
                    "q": query,
                    "key": settings.tenor_api_key,
                    "limit": limit,
                    "media_filter": "gif,tinygif",
                    "contentfilter": "medium",
                    "ar_range": "all",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        gifs = []
        for result in data.get("results", []):
            title = result.get("title") or result.get("content_description", "")
            formats = result.get("media_formats", {})

            # Prefer tinygif (smaller, faster upload) for tweets; fall back to gif
            gif_data = formats.get("tinygif") or formats.get("gif", {})
            url = gif_data.get("url", "")

            if url:
                gifs.append({
                    "url": url,
                    "title": title,
                    "dims": gif_data.get("dims", []),
                })

        logger.info("gif_search", query=query, results=len(gifs))

        return {
            "query": query,
            "gifs": gifs,
            "count": len(gifs),
        }

    def get_schema(self) -> dict:
        """JSON schema for find_gif inputs."""
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Description of the GIF (e.g. 'mind blown', 'awkward silence', 'this is fine')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of results (1-10, default 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        }
