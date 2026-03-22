"""
X (Twitter) unfollow skill.

Unfollows a user on X from @astrlboy_. Rate-limited to 20 unfollows per day
via a Redis counter with 24-hour TTL. Every unfollow is logged with the reason
to maintain an audit trail of growth strategy adjustments.
"""

from typing import Any

import tweepy

from cache.redis import redis_client
from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.unfollow_x")

# Daily unfollow cap — mirrors the follow cap to avoid triggering
# X's anti-churn detection. Aggressive unfollowing looks like bot behavior.
DAILY_UNFOLLOW_LIMIT = 20
REDIS_KEY_UNFOLLOWS_TODAY = "astrlboy:unfollows_today"
REDIS_TTL_ONE_DAY = 86400


class UnfollowXSkill(BaseTool):
    """Unfollow a user on X via OAuth 1.0a.

    Uses a Redis counter to enforce a hard daily limit of 20 unfollows.
    This keeps the agent's follow/unfollow churn within safe bounds
    and prevents X from flagging the account for manipulation.
    """

    name = "unfollow_x"
    description = (
        "Unfollow a user on X (Twitter). Rate-limited to 20 unfollows per day. "
        "Use for pruning low-value follows to keep the following list focused."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        self._client = tweepy.Client(
            consumer_key=settings.twitter_api_key,
            consumer_secret=settings.twitter_api_secret,
            access_token=settings.twitter_access_token,
            access_token_secret=settings.twitter_access_secret,
        )

    async def _check_and_increment_counter(self) -> int:
        """Increment the daily unfollow counter and return the new count.

        Sets a 24-hour TTL on first increment of the day so the counter
        resets automatically.

        Returns:
            The new count after incrementing.

        Raises:
            SkillExecutionError: If the daily limit has been reached.
        """
        if redis_client is None:
            logger.warning("redis_not_configured", skill="unfollow_x")
            return 1

        count = await redis_client.incr(REDIS_KEY_UNFOLLOWS_TODAY)

        # First unfollow of the day — set expiry so counter resets in 24h
        if count == 1:
            await redis_client.expire(REDIS_KEY_UNFOLLOWS_TODAY, REDIS_TTL_ONE_DAY)

        if count > DAILY_UNFOLLOW_LIMIT:
            logger.warning(
                "daily_unfollow_limit_reached",
                count=count,
                limit=DAILY_UNFOLLOW_LIMIT,
            )
            await redis_client.decr(REDIS_KEY_UNFOLLOWS_TODAY)
            raise SkillExecutionError(
                f"Daily unfollow limit reached ({DAILY_UNFOLLOW_LIMIT})"
            )

        return count

    async def execute(
        self,
        user_id: str,
        reason: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Unfollow a user on X.

        Args:
            user_id: The X user ID to unfollow.
            reason: Why we're unfollowing this account (logged for audit trail).

        Returns:
            Dict with 'following' boolean (should be False after success).

        Raises:
            SkillExecutionError: If the daily limit is reached or the API call fails.
        """
        count = await self._check_and_increment_counter()

        try:
            response = self._client.unfollow_user(target_user_id=user_id)
            data = response.data or {}
            following = data.get("following", False)

            logger.info(
                "user_unfollowed",
                user_id=user_id,
                reason=reason,
                following=following,
                daily_count=count,
            )

            return {"following": following}
        except Exception as exc:
            logger.error(
                "unfollow_x_failed",
                user_id=user_id,
                reason=reason,
                error=str(exc),
            )
            raise SkillExecutionError(f"X unfollow failed: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for unfollow_x inputs."""
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "X user ID to unfollow",
                },
                "reason": {
                    "type": "string",
                    "description": "Why we're unfollowing (logged for audit)",
                },
            },
            "required": ["user_id"],
        }
