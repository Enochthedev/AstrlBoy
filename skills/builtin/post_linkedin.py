"""
LinkedIn posting skill.

Posts to LinkedIn via the Posts API. Used for professional content
publishing on behalf of clients.
"""

from typing import Any

import httpx

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.post_linkedin")

LINKEDIN_API_URL = "https://api.linkedin.com/v2"


class PostLinkedInSkill(BaseTool):
    """Post content to LinkedIn."""

    name = "post_linkedin"
    description = "Post content to LinkedIn. Supports text posts."
    version = "1.0.0"

    async def execute(
        self,
        text: str,
        author_urn: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Post to LinkedIn.

        Args:
            text: The post text.
            author_urn: LinkedIn URN for the author. Uses default if not provided.

        Returns:
            Dict with 'post_id' of the created post.

        Raises:
            SkillExecutionError: If posting fails.
        """
        try:
            async with httpx.AsyncClient() as client:
                # Get the user's URN if not provided
                if not author_urn:
                    me_response = await client.get(
                        f"{LINKEDIN_API_URL}/userinfo",
                        headers={"Authorization": f"Bearer {settings.linkedin_access_token}"},
                    )
                    me_response.raise_for_status()
                    author_urn = f"urn:li:person:{me_response.json()['sub']}"

                response = await client.post(
                    f"{LINKEDIN_API_URL}/ugcPosts",
                    headers={
                        "Authorization": f"Bearer {settings.linkedin_access_token}",
                        "Content-Type": "application/json",
                        "X-Restli-Protocol-Version": "2.0.0",
                    },
                    json={
                        "author": author_urn,
                        "lifecycleState": "PUBLISHED",
                        "specificContent": {
                            "com.linkedin.ugc.ShareContent": {
                                "shareCommentary": {"text": text},
                                "shareMediaCategory": "NONE",
                            }
                        },
                        "visibility": {
                            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
                        },
                    },
                )
                response.raise_for_status()
                post_id = response.headers.get("x-restli-id", "unknown")
                logger.info("linkedin_post_created", post_id=post_id)
                return {"post_id": post_id, "text": text}
        except Exception as exc:
            logger.error("post_linkedin_failed", error=str(exc))
            raise SkillExecutionError(f"LinkedIn post failed: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for post_linkedin inputs."""
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Post text"},
                "author_urn": {"type": "string", "description": "LinkedIn author URN"},
            },
            "required": ["text"],
        }
