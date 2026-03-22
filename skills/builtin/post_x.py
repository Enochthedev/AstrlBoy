"""
X (Twitter) posting skill.

Posts tweets via OAuth 1.0a. Used for autonomous content publishing
and community engagement on X.
"""

from typing import Any

import tweepy

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.post_x")


class PostXSkill(BaseTool):
    """Post a tweet to X via OAuth 1.0a."""

    name = "post_x"
    description = "Post a tweet to X (Twitter). Supports text tweets and replies."
    version = "1.0.0"

    def __init__(self) -> None:
        self._client = tweepy.Client(
            consumer_key=settings.twitter_api_key,
            consumer_secret=settings.twitter_api_secret,
            access_token=settings.twitter_access_token,
            access_token_secret=settings.twitter_access_secret,
        )

    async def execute(
        self,
        text: str,
        reply_to_id: str | None = None,
        quote_tweet_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Post a tweet.

        Args:
            text: The tweet text (max 280 characters).
            reply_to_id: Optional tweet ID to reply to.
            quote_tweet_id: Optional tweet ID to quote.

        Returns:
            Dict with 'tweet_id' and 'text' of the posted tweet.

        Raises:
            SkillExecutionError: If posting fails.
        """
        try:
            response = self._client.create_tweet(
                text=text,
                in_reply_to_tweet_id=reply_to_id,
                quote_tweet_id=quote_tweet_id,
            )
            tweet_id = response.data["id"]
            logger.info("tweet_posted", tweet_id=tweet_id)
            return {"tweet_id": tweet_id, "text": text}
        except Exception as exc:
            logger.error("post_x_failed", error=str(exc))
            raise SkillExecutionError(f"X post failed: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for post_x inputs."""
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Tweet text (max 280 chars)"},
                "reply_to_id": {"type": "string", "description": "Tweet ID to reply to"},
                "quote_tweet_id": {"type": "string", "description": "Tweet ID to quote"},
            },
            "required": ["text"],
        }
