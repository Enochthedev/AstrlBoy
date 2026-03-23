"""
X (Twitter) delete tweet skill.

Deletes one of astrlboy's own tweets. Use for cleanup — removing tweets
that performed poorly, contained errors, or are no longer relevant.

Only works on tweets posted by the authenticated account. Attempting to
delete someone else's tweet returns a 403.
"""

import asyncio
from typing import Any

import tweepy

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.delete_tweet_x")

_MAX_RETRIES = 3
_BASE_DELAY = 1.0


class DeleteTweetXSkill(BaseTool):
    """Delete one of astrlboy's own tweets on X.

    Only works on tweets posted by the authenticated account.
    Use for cleaning up errors, poor performers, or stale content.
    """

    name = "delete_tweet_x"
    description = (
        "Delete one of astrlboy's own tweets. Only works on tweets "
        "posted by this account. Use for cleanup."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        self._client = tweepy.Client(
            consumer_key=settings.twitter_api_key,
            consumer_secret=settings.twitter_api_secret,
            access_token=settings.twitter_access_token,
            access_token_secret=settings.twitter_access_secret,
        )

    async def execute(self, tweet_id: str, **kwargs: Any) -> dict[str, Any]:
        """Delete a tweet.

        Args:
            tweet_id: ID of the tweet to delete (must be own tweet).

        Returns:
            Dict with 'tweet_id' and 'deleted' status.

        Raises:
            SkillExecutionError: If deletion fails after retries.
        """
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self._client.delete_tweet(tweet_id)
                logger.info("tweet_deleted", tweet_id=tweet_id)
                return {"tweet_id": tweet_id, "deleted": True}

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "delete_tweet_retry",
                    tweet_id=tweet_id,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_BASE_DELAY * (2 ** (attempt - 1)))

        raise SkillExecutionError(
            f"Delete tweet {tweet_id} failed after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    def get_schema(self) -> dict:
        """Return JSON schema for delete_tweet_x inputs."""
        return {
            "type": "object",
            "properties": {
                "tweet_id": {
                    "type": "string",
                    "description": "ID of the tweet to delete (must be your own)",
                },
            },
            "required": ["tweet_id"],
        }
