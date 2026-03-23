"""
X (Twitter) retweet skill.

Retweets a tweet — amplifies content without creating original text.
Use this when a tweet aligns with the client's positioning and you want
to boost it to astrlboy's audience. Costs one tweet write on the API.

For adding commentary, use post_x with quote_tweet_id instead.
Retweets count toward the daily tweet cap.
"""

import asyncio
from typing import Any

import tweepy

from core.budget import XOperation, budget_tracker
from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.retweet_x")

_MAX_RETRIES = 3
_BASE_DELAY = 1.0


class RetweetXSkill(BaseTool):
    """Retweet a tweet on X.

    Amplifies someone else's content to astrlboy's followers.
    For adding your own commentary, use post_x with quote_tweet_id.
    """

    name = "retweet_x"
    description = (
        "Retweet a tweet on X. Amplifies content without adding text. "
        "For quote tweets with commentary, use post_x with quote_tweet_id."
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
        """Retweet a tweet.

        Args:
            tweet_id: ID of the tweet to retweet.

        Returns:
            Dict with 'tweet_id' and 'retweeted' status.

        Raises:
            SkillExecutionError: If retweeting fails after retries.
        """
        # Check daily tweet cap — retweets count as writes
        if budget_tracker:
            can_tweet = await budget_tracker.check_tweet_budget()
            if not can_tweet:
                count = await budget_tracker.get_tweet_count_today()
                raise SkillExecutionError(
                    f"Daily tweet cap reached ({count}/{budget_tracker.daily_tweet_cap}). "
                    "Cannot retweet."
                )

        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self._client.retweet(tweet_id)

                if budget_tracker:
                    await budget_tracker.increment_tweet_count()
                    await budget_tracker.track(XOperation.POST_CREATE)

                logger.info("tweet_retweeted", tweet_id=tweet_id)
                return {"tweet_id": tweet_id, "retweeted": True}

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "retweet_x_retry",
                    tweet_id=tweet_id,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_BASE_DELAY * (2 ** (attempt - 1)))

        raise SkillExecutionError(
            f"Retweet {tweet_id} failed after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    def get_schema(self) -> dict:
        """Return JSON schema for retweet_x inputs."""
        return {
            "type": "object",
            "properties": {
                "tweet_id": {
                    "type": "string",
                    "description": "ID of the tweet to retweet",
                },
            },
            "required": ["tweet_id"],
        }
