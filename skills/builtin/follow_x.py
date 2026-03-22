"""
X (Twitter) follow skill.

Follows a user on X from @astrlboy_. Rate-limited to 20 follows per day
via a Redis counter with 24-hour TTL. Every follow is logged with the reason
so we can audit growth strategy decisions later.
"""

from typing import Any

import tweepy

from cache.redis import redis_client
from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.follow_x")

# Daily follow cap — X rate limits are strict, and aggressive following
# gets accounts flagged. 20/day is conservative and sustainable.
DAILY_FOLLOW_LIMIT = 20
REDIS_KEY_FOLLOWS_TODAY = "astrlboy:follows_today"
REDIS_TTL_ONE_DAY = 86400


class FollowXSkill(BaseTool):
    """Follow a user on X via OAuth 1.0a.

    Uses a Redis counter to enforce a hard daily limit of 20 follows.
    This prevents the agent from tripping X's anti-spam heuristics
    even if multiple graphs trigger follows concurrently.
    """

    name = "follow_x"
    description = (
        "Follow a user on X (Twitter). Rate-limited to 20 follows per day. "
        "Use for strategic growth — following relevant accounts in a client's niche."
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
        """Increment the daily follow counter and return the new count.

        Sets a 24-hour TTL on first increment of the day so the counter
        resets automatically.

        Returns:
            The new count after incrementing.

        Raises:
            SkillExecutionError: If the daily limit has been reached.
        """
        if redis_client is None:
            # No Redis in dev — allow but warn
            logger.warning("redis_not_configured", skill="follow_x")
            return 1

        count = await redis_client.incr(REDIS_KEY_FOLLOWS_TODAY)

        # First follow of the day — set expiry so counter resets in 24h
        if count == 1:
            await redis_client.expire(REDIS_KEY_FOLLOWS_TODAY, REDIS_TTL_ONE_DAY)

        if count > DAILY_FOLLOW_LIMIT:
            logger.warning(
                "daily_follow_limit_reached",
                count=count,
                limit=DAILY_FOLLOW_LIMIT,
            )
            # Decrement back since we won't actually follow
            await redis_client.decr(REDIS_KEY_FOLLOWS_TODAY)
            raise SkillExecutionError(
                f"Daily follow limit reached ({DAILY_FOLLOW_LIMIT})"
            )

        return count

    async def execute(
        self,
        user_id: str,
        reason: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Follow a user on X.

        Args:
            user_id: The X user ID to follow.
            reason: Why we're following this account (logged, not sent to X).

        Returns:
            Dict with 'following' and 'pending_follow' booleans.

        Raises:
            SkillExecutionError: If the daily limit is reached or the API call fails.
        """
        count = await self._check_and_increment_counter()

        try:
            response = self._client.follow_user(target_user_id=user_id)
            data = response.data or {}
            following = data.get("following", False)
            pending = data.get("pending_follow", False)

            logger.info(
                "user_followed",
                user_id=user_id,
                reason=reason,
                following=following,
                pending_follow=pending,
                daily_count=count,
            )

            return {
                "following": following,
                "pending_follow": pending,
            }
        except Exception as exc:
            logger.error(
                "follow_x_failed",
                user_id=user_id,
                reason=reason,
                error=str(exc),
            )
            raise SkillExecutionError(f"X follow failed: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for follow_x inputs."""
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "X user ID to follow",
                },
                "reason": {
                    "type": "string",
                    "description": "Why we're following (logged, not sent to X)",
                },
            },
            "required": ["user_id"],
        }
