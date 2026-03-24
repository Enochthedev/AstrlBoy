"""
Meme generation skill using imgflip.

Creates captioned meme images from classic templates (Drake, Distracted Boyfriend,
This Is Fine, Stonks, etc.) via the imgflip API. Returns an image URL you can
attach to a tweet with post_x's media_url parameter.

Free tier: no daily limit on meme creation, just needs an imgflip account.
Requires: IMGFLIP_USERNAME + IMGFLIP_PASSWORD env vars.
"""

from typing import Any

import httpx

from core.config import settings
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.generate_meme")

_IMGFLIP_BASE = "https://api.imgflip.com"

# Popular meme templates — name → imgflip template ID
# Full list at api.imgflip.com/get_memes but these are the most reliably recognizable
MEME_TEMPLATES: dict[str, str] = {
    "drake":                    "181913649",   # Drake approving/disapproving
    "distracted boyfriend":     "112126428",   # Guy looking at other woman
    "two buttons":              "87743020",    # Sweating over two buttons
    "change my mind":           "129242436",   # Steven Crowder change my mind
    "this is fine":             "55311130",    # Dog in burning room
    "stonks":                   "195389459",   # Meme Man stonks
    "not stonks":               "195389459",   # Same template
    "galaxy brain":             "93895088",    # Expanding brain
    "they're the same picture": "180190441",   # Pam from The Office
    "uno reverse":              "438680",      # Uno reverse card
    "gru plan":                 "131940431",   # Gru's plan steps
    "always has been":          "252600902",   # Astronaut always has been
    "is this a pigeon":         "100777631",   # Anime butterfly meme
    "doge":                     "8072285",     # Such wow doge
    "success kid":              "61544",       # Fist pump success
    "y u no":                   "61527",       # Y U NO guy
    "10 guy":                   "19967396",    # Stoner 10 guy
    "evil kermit":              "84341851",    # Evil Kermit temptation
    "one does not simply":      "61579",       # Boromir meme
    "shut up and take my money": "10silverware", # Fry shut up and take
}


class GenerateMemeSkill(BaseTool):
    """Generate a captioned meme image using imgflip.

    Choose a template by name (e.g. 'drake', 'distracted boyfriend', 'this is fine')
    and provide the caption text. Returns an image URL to attach to a tweet.

    Use when you want to make a point with humor — contrasting two things, reacting
    to a situation, or adding a recognizable meme format to a take.
    """

    name = "generate_meme"
    description = (
        "Generate a captioned meme image from a classic template (Drake, Distracted Boyfriend, "
        "This Is Fine, Stonks, Galaxy Brain, etc.). "
        "Provide the template name and text for each caption box. "
        "Returns an image URL — pass it to post_x as media_url to attach to a tweet. "
        "Best for: making a contrasting point, reacting to something with humor, "
        "or when text alone won't land the joke."
    )
    version = "1.0.0"

    async def execute(
        self,
        template: str,
        text0: str,
        text1: str = "",
        text2: str = "",
        text3: str = "",
    ) -> dict[str, Any]:
        """Generate a meme image.

        Args:
            template: Template name (e.g. 'drake', 'this is fine', 'galaxy brain').
                      Call with template='list' to see all available templates.
            text0: Text for the first caption box (top or first panel).
            text1: Text for the second box (bottom or second panel).
            text2: Text for third box if template has more panels (e.g. gru plan).
            text3: Text for fourth box if template has more panels.

        Returns:
            Dict with 'url' (direct image URL) and 'template' used.
        """
        if not settings.imgflip_username or not settings.imgflip_password:
            return {
                "status": "unavailable",
                "reason": "IMGFLIP_USERNAME and IMGFLIP_PASSWORD not configured",
            }

        if template == "list":
            return {
                "templates": list(MEME_TEMPLATES.keys()),
                "tip": "Pass one of these names as the 'template' parameter",
            }

        # Find template ID — exact match first, then partial
        template_lower = template.lower().strip()
        template_id = MEME_TEMPLATES.get(template_lower)

        if not template_id:
            # Try partial match
            for name, tid in MEME_TEMPLATES.items():
                if template_lower in name or name in template_lower:
                    template_id = tid
                    break

        if not template_id:
            return {
                "status": "not_found",
                "template": template,
                "available": list(MEME_TEMPLATES.keys()),
                "tip": "Try a template from the 'available' list, or use template='list' to see all",
            }

        boxes = [{"text": t} for t in [text0, text1, text2, text3] if t]

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_IMGFLIP_BASE}/caption_image",
                data={
                    "template_id": template_id,
                    "username": settings.imgflip_username,
                    "password": settings.imgflip_password,
                    "boxes[0][text]": text0,
                    "boxes[1][text]": text1,
                    "boxes[2][text]": text2,
                    "boxes[3][text]": text3,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        if not data.get("success"):
            error = data.get("error_message", "imgflip API error")
            logger.warning("meme_generation_failed", template=template, error=error)
            return {"status": "failed", "error": error}

        url = data["data"]["url"]
        logger.info("meme_generated", template=template, url=url)

        return {
            "url": url,
            "template": template,
            "captions": [t for t in [text0, text1, text2, text3] if t],
        }

    def get_schema(self) -> dict:
        """JSON schema for generate_meme inputs."""
        return {
            "type": "object",
            "properties": {
                "template": {
                    "type": "string",
                    "description": (
                        "Meme template name. Options: drake, distracted boyfriend, two buttons, "
                        "change my mind, this is fine, stonks, galaxy brain, they're the same picture, "
                        "gru plan, always has been, is this a pigeon, doge, evil kermit, one does not simply. "
                        "Pass 'list' to see all options."
                    ),
                },
                "text0": {
                    "type": "string",
                    "description": "First caption (top panel or disapproval side for drake)",
                },
                "text1": {
                    "type": "string",
                    "description": "Second caption (bottom panel or approval side for drake)",
                },
                "text2": {
                    "type": "string",
                    "description": "Third caption (for multi-panel templates like gru plan or galaxy brain)",
                },
                "text3": {
                    "type": "string",
                    "description": "Fourth caption (for 4-panel templates like galaxy brain)",
                },
            },
            "required": ["template", "text0"],
        }
