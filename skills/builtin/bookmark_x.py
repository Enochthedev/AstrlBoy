"""
X (Twitter) bookmark skill.

Bookmarks a tweet for later reference. Bookmarks are private — only visible
to astrlboy. Use this to save tweets that contain useful information for
research, content ideas, or competitor monitoring.

The intelligence graph can bookmark competitor announcements, and the
content graph can bookmark high-performing tweets as reference material.
"""

import asyncio
from typing import Any

import tweepy

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.bookmark_x")

_MAX_RETRIES = 3
_BASE_DELAY = 1.0


class BookmarkXSkill(BaseTool):
    """Bookmark a tweet on X for later reference.

    Bookmarks are private — only visible to the authenticated account.
    Use to save interesting tweets for research, content ideas, or
    competitor tracking without publicly engaging.
    """

    name = "bookmark_x"
    description = (
        "Bookmark a tweet on X for later reference. Private — only visible "
        "to astrlboy. Use for saving research, content ideas, competitor posts."
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

    async def execute(self, tweet_id: str, **kwargs: Any) -> dict[str, Any]:
        """Bookmark a tweet.

        Args:
            tweet_id: ID of the tweet to bookmark.

        Returns:
            Dict with 'tweet_id' and 'bookmarked' status.

        Raises:
            SkillExecutionError: If bookmarking fails after retries.
        """
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self._client.bookmark(tweet_id)
                logger.info("tweet_bookmarked", tweet_id=tweet_id)
                return {"tweet_id": tweet_id, "bookmarked": True}

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "bookmark_x_retry",
                    tweet_id=tweet_id,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_BASE_DELAY * (2 ** (attempt - 1)))

        raise SkillExecutionError(
            f"Bookmark tweet {tweet_id} failed after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    def get_schema(self) -> dict:
        """Return JSON schema for bookmark_x inputs."""
        return {
            "type": "object",
            "properties": {
                "tweet_id": {
                    "type": "string",
                    "description": "ID of the tweet to bookmark",
                },
            },
            "required": ["tweet_id"],
        }
