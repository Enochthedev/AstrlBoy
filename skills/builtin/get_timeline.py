"""
X timeline skill.

Fetches astrlboy's own recent tweets so the agent can avoid repeating content
and maintain awareness of what it has already posted.
"""

from typing import Any

import tweepy

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.get_timeline")


class GetTimelineSkill(BaseTool):
    """Fetch astrlboy's own recent tweets."""

    name = "get_timeline"
    description = "Get astrlboy's own recent tweets to check what was already posted."
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
        max_results: int = 10,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch recent tweets from the authenticated account.

        Args:
            max_results: Number of recent tweets to return (5-100).

        Returns:
            List of tweet dicts with id and text.

        Raises:
            SkillExecutionError: If the API call fails.
        """
        try:
            me = self._client.get_me()
            if not me or not me.data:
                raise SkillExecutionError("Could not get authenticated user")

            response = self._client.get_users_tweets(
                id=me.data.id,
                max_results=max(5, min(max_results, 100)),
                tweet_fields=["created_at", "text"],
            )

            if not response or not response.data:
                return []

            tweets = []
            for tweet in response.data:
                tweets.append({
                    "id": str(tweet.id),
                    "text": tweet.text,
                    "created_at": str(tweet.created_at) if tweet.created_at else None,
                })

            logger.info("timeline_fetched", count=len(tweets))
            return tweets
        except SkillExecutionError:
            raise
        except Exception as exc:
            logger.error("get_timeline_failed", error=str(exc))
            raise SkillExecutionError(f"Get timeline failed: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for get_timeline inputs."""
        return {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "description": "Number of tweets (5-100)"},
            },
            "required": [],
        }
