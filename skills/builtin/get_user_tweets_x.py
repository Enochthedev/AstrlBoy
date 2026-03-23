"""
X (Twitter) get user tweets skill.

Fetches recent tweets from a specific user's profile. Different from
get_timeline which fetches the home timeline (algorithmic feed).
This is for reading what a specific account has posted recently.

Use cases:
- OSINT before engaging with or following an account
- Competitor monitoring (what are they tweeting about?)
- Pre-application research (what does the hiring company post?)
- Understanding an account's tone before replying
"""

import asyncio
from typing import Any

import tweepy

from core.budget import XOperation, budget_tracker
from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.get_user_tweets_x")

_MAX_RETRIES = 3
_BASE_DELAY = 1.0


class GetUserTweetsXSkill(BaseTool):
    """Fetch recent tweets from a specific X user's profile.

    Returns the user's recent original tweets (excludes retweets by default).
    Useful for researching accounts before engaging, following, or applying.
    """

    name = "get_user_tweets_x"
    description = (
        "Fetch recent tweets from a specific X user. Returns their original "
        "tweets with metrics. Use for research before engaging or following."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        # Bearer token for read-only access
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
        max_results: int = 10,
        exclude_replies: bool = True,
        exclude_retweets: bool = True,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch a user's recent tweets.

        Args:
            username: X username (without @). Either this or user_id required.
            user_id: X user ID. Either this or username required.
            max_results: Number of tweets to fetch (5-100).
            exclude_replies: If True, exclude replies from results.
            exclude_retweets: If True, exclude retweets from results.

        Returns:
            List of tweet dicts with 'id', 'text', 'created_at', 'metrics'.

        Raises:
            SkillExecutionError: If fetching fails after retries.
        """
        if not username and not user_id:
            raise SkillExecutionError("Either username or user_id is required")

        # Resolve username to user_id if needed
        if username and not user_id:
            try:
                user_resp = self._client.get_user(username=username)
                if not user_resp.data:
                    raise SkillExecutionError(f"User @{username} not found")
                user_id = str(user_resp.data.id)

                if budget_tracker:
                    await budget_tracker.track(XOperation.USER_LOOKUP, count=1)
            except SkillExecutionError:
                raise
            except Exception as exc:
                raise SkillExecutionError(f"User lookup failed for @{username}: {exc}") from exc

        # Build exclude list
        exclude = []
        if exclude_replies:
            exclude.append("replies")
        if exclude_retweets:
            exclude.append("retweets")

        last_exc: Exception | None = None
        max_results = max(5, min(max_results, 100))

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = self._client.get_users_tweets(
                    id=user_id,
                    max_results=max_results,
                    tweet_fields=["created_at", "public_metrics", "conversation_id"],
                    exclude=exclude if exclude else None,
                )

                if budget_tracker:
                    count = len(response.data) if response.data else 0
                    await budget_tracker.track(XOperation.POST_READ, count=max(count, 1))

                if not response.data:
                    logger.info("no_tweets_found", user_id=user_id)
                    return []

                tweets = [
                    {
                        "id": str(t.id),
                        "text": t.text,
                        "created_at": str(t.created_at) if t.created_at else None,
                        "metrics": dict(t.public_metrics) if t.public_metrics else {},
                    }
                    for t in response.data
                ]

                logger.info(
                    "user_tweets_fetched",
                    user_id=user_id,
                    username=username,
                    count=len(tweets),
                )
                return tweets

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "get_user_tweets_retry",
                    user_id=user_id,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_BASE_DELAY * (2 ** (attempt - 1)))

        raise SkillExecutionError(
            f"Get tweets for user {user_id or username} failed "
            f"after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    def get_schema(self) -> dict:
        """Return JSON schema for get_user_tweets_x inputs."""
        return {
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "X username without @ (e.g. 'elonmusk')",
                },
                "user_id": {
                    "type": "string",
                    "description": "X user ID (alternative to username)",
                },
                "max_results": {
                    "type": "integer",
                    "default": 10,
                    "description": "Number of tweets to fetch (5-100)",
                },
                "exclude_replies": {
                    "type": "boolean",
                    "default": True,
                    "description": "Exclude replies from results",
                },
                "exclude_retweets": {
                    "type": "boolean",
                    "default": True,
                    "description": "Exclude retweets from results",
                },
            },
            "required": [],
        }
