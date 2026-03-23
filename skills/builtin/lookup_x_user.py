"""
X user lookup skill.

Resolves usernames to IDs, fetches profile info, and checks follower counts.
Essential for the agent to make informed decisions before following, replying,
or engaging with accounts.
"""

from typing import Any

import tweepy

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.lookup_x_user")


class LookupXUserSkill(BaseTool):
    """Look up an X user's profile by username or ID."""

    name = "lookup_x_user"
    description = (
        "Look up an X user by username or ID. Returns their numeric ID, "
        "display name, bio, follower/following counts, and verified status. "
        "Use before following or engaging to check if the account is worth it."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        self._client = tweepy.Client(
            bearer_token=settings.twitter_bearer_token,
            consumer_key=settings.twitter_api_key,
            consumer_secret=settings.twitter_api_secret,
            access_token=settings.twitter_access_token,
            access_token_secret=settings.twitter_access_secret,
        )

    async def execute(
        self,
        username: str | None = None,
        user_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Look up an X user.

        Args:
            username: The username (without @) to look up.
            user_id: The numeric user ID to look up.

        Returns:
            Dict with id, username, name, bio, followers, following, verified.

        Raises:
            SkillExecutionError: If the lookup fails.
        """
        if not username and not user_id:
            raise SkillExecutionError("Provide either username or user_id")

        try:
            if username:
                clean = username.strip().lstrip("@")
                response = self._client.get_user(
                    username=clean,
                    user_fields=["description", "public_metrics", "verified", "created_at"],
                )
            else:
                response = self._client.get_user(
                    id=user_id,
                    user_fields=["description", "public_metrics", "verified", "created_at"],
                )

            if not response or not response.data:
                raise SkillExecutionError(f"User not found: {username or user_id}")

            user = response.data
            metrics = user.public_metrics or {}

            # Track cost
            try:
                from core.budget import XOperation, budget_tracker
                if budget_tracker:
                    await budget_tracker.track(XOperation.USER_LOOKUP)
            except Exception:
                pass

            result = {
                "id": str(user.id),
                "username": user.username,
                "name": user.name,
                "bio": user.description or "",
                "followers": metrics.get("followers_count", 0),
                "following": metrics.get("following_count", 0),
                "tweets": metrics.get("tweet_count", 0),
                "verified": getattr(user, "verified", False),
                "created_at": str(user.created_at) if user.created_at else None,
            }

            logger.info("x_user_looked_up", username=result["username"], user_id=result["id"])
            return result

        except SkillExecutionError:
            raise
        except Exception as exc:
            logger.error("lookup_x_user_failed", target=username or user_id, error=str(exc))
            raise SkillExecutionError(f"User lookup failed: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for lookup_x_user inputs."""
        return {
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "X username to look up (without @)",
                },
                "user_id": {
                    "type": "string",
                    "description": "Numeric X user ID to look up",
                },
            },
            "required": [],
        }
