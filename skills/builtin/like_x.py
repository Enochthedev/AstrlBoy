"""
X (Twitter) like skill.

Likes a tweet — the lightest form of engagement. Costs nothing in terms
of daily tweet cap (likes are free on the X API), but signals interest
to the algorithm and the tweet author. Use this when a reply would be
too much but you still want to register engagement.

The engagement graph should like high-score threads even if it doesn't
draft a reply — it's a low-cost way to stay visible.
"""

import asyncio
from typing import Any

import tweepy

from core.budget import XOperation, budget_tracker
from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.like_x")

_MAX_RETRIES = 3
_BASE_DELAY = 1.0


class LikeXSkill(BaseTool):
    """Like a tweet on X.

    Lightweight engagement that doesn't count toward the daily tweet cap.
    Use this alongside or instead of replies for threads that are worth
    acknowledging but don't warrant a full response.
    """

    name = "like_x"
    description = (
        "Like a tweet on X. Lightweight engagement — doesn't count toward "
        "the daily tweet cap. Use for signaling interest without replying."
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
        """Like a tweet.

        Args:
            tweet_id: ID of the tweet to like.

        Returns:
            Dict with 'tweet_id' and 'liked' status.

        Raises:
            SkillExecutionError: If liking fails after retries.
        """
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self._client.like(tweet_id)

                if budget_tracker:
                    await budget_tracker.track(XOperation.POST_CREATE, count=1)

                logger.info("tweet_liked", tweet_id=tweet_id)
                return {"tweet_id": tweet_id, "liked": True}

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "like_x_retry",
                    tweet_id=tweet_id,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_BASE_DELAY * (2 ** (attempt - 1)))

        raise SkillExecutionError(
            f"Like tweet {tweet_id} failed after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    def get_schema(self) -> dict:
        """Return JSON schema for like_x inputs."""
        return {
            "type": "object",
            "properties": {
                "tweet_id": {
                    "type": "string",
                    "description": "ID of the tweet to like",
                },
            },
            "required": ["tweet_id"],
        }
