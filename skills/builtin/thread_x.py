"""
X (Twitter) thread posting skill.

Posts multi-tweet threads by chaining replies to the agent's own tweets.
X API v2 has no native "thread" endpoint — threads are just a sequence of
tweets where each replies to the previous one using in_reply_to_tweet_id.

Each tweet in the thread counts toward the daily tweet cap, so graphs should
be selective about thread length. 3-5 tweets is the sweet spot for reach.
"""

import asyncio
from typing import Any

import tweepy

from core.budget import XOperation, budget_tracker
from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.thread_x")

# Retry config — 3 attempts with exponential backoff per tweet
_MAX_RETRIES = 3
_BASE_DELAY = 1.0

# X API v2 enforces a short delay between rapid-fire creates to avoid
# 429s. 2 seconds between tweets in a thread is safe and looks natural.
_INTER_TWEET_DELAY = 2.0


class ThreadXSkill(BaseTool):
    """Post a multi-tweet thread on X by chaining replies.

    Takes a list of tweet texts and posts them sequentially, each replying
    to the previous. The first tweet is a standalone post. All tweets in
    the thread count toward the daily cap.

    Returns the full list of posted tweet IDs and URLs so the calling graph
    can log the thread for training data.
    """

    name = "thread_x"
    description = (
        "Post a multi-tweet thread on X. Takes a list of tweet texts, "
        "posts the first as a standalone tweet, then chains each subsequent "
        "tweet as a reply. Returns all tweet IDs."
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

    async def _post_single(
        self, text: str, reply_to_id: str | None = None
    ) -> dict[str, str]:
        """Post a single tweet with retry logic.

        Args:
            text: Tweet text (max 280 characters).
            reply_to_id: If set, posts as a reply to this tweet ID.

        Returns:
            Dict with 'tweet_id' of the posted tweet.

        Raises:
            SkillExecutionError: If posting fails after all retries.
        """
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = self._client.create_tweet(
                    text=text,
                    in_reply_to_tweet_id=reply_to_id,
                )
                tweet_id = response.data["id"]

                if budget_tracker:
                    await budget_tracker.increment_tweet_count()
                    await budget_tracker.track(XOperation.POST_CREATE)

                return {"tweet_id": tweet_id}

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "thread_tweet_retry",
                    attempt=attempt,
                    reply_to=reply_to_id,
                    error=str(exc),
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_BASE_DELAY * (2 ** (attempt - 1)))

        raise SkillExecutionError(
            f"Thread tweet failed after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    async def execute(
        self,
        tweets: list[str],
        bypass_cap: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Post a thread on X.

        Args:
            tweets: List of tweet texts, in order. Each max 280 chars.
                Must have at least 2 tweets (otherwise just use post_x).
            bypass_cap: If True, skip daily tweet cap checks.

        Returns:
            Dict with:
                - 'thread_tweet_ids': list of all posted tweet IDs in order
                - 'thread_url': URL to the first tweet (thread root)
                - 'count': number of tweets posted
                - 'partial': True if the thread was partially posted (some failed)

        Raises:
            SkillExecutionError: If the first tweet fails or cap is reached.
        """
        if not tweets or len(tweets) < 2:
            raise SkillExecutionError(
                "Thread requires at least 2 tweets. Use post_x for single tweets."
            )

        # Validate all tweet lengths upfront before posting anything
        for i, text in enumerate(tweets):
            if len(text) > 280:
                raise SkillExecutionError(
                    f"Tweet {i + 1} is {len(text)} chars (max 280): "
                    f"{text[:50]}..."
                )

        # Check daily cap for the entire thread before starting
        if not bypass_cap and budget_tracker:
            can_tweet = await budget_tracker.check_tweet_budget()
            if not can_tweet:
                count = await budget_tracker.get_tweet_count_today()
                raise SkillExecutionError(
                    f"Daily tweet cap reached ({count}/{budget_tracker.daily_tweet_cap}). "
                    f"Cannot post {len(tweets)}-tweet thread."
                )

        posted_ids: list[str] = []
        previous_id: str | None = None

        for i, text in enumerate(tweets):
            try:
                # Delay between tweets to avoid rate limits and look natural
                if i > 0:
                    await asyncio.sleep(_INTER_TWEET_DELAY)

                result = await self._post_single(text, reply_to_id=previous_id)
                tweet_id = result["tweet_id"]
                posted_ids.append(tweet_id)
                previous_id = tweet_id

                logger.info(
                    "thread_tweet_posted",
                    position=i + 1,
                    total=len(tweets),
                    tweet_id=tweet_id,
                )

            except SkillExecutionError:
                # If the first tweet fails, the whole thread fails
                if i == 0:
                    raise

                # If a later tweet fails, return what we have (partial thread)
                logger.warning(
                    "thread_partial",
                    posted=len(posted_ids),
                    total=len(tweets),
                    failed_at=i + 1,
                )
                return {
                    "thread_tweet_ids": posted_ids,
                    "thread_url": f"https://x.com/{settings.agent_handle.lstrip('@')}/status/{posted_ids[0]}",
                    "count": len(posted_ids),
                    "partial": True,
                }

        thread_url = f"https://x.com/{settings.agent_handle.lstrip('@')}/status/{posted_ids[0]}"

        logger.info(
            "thread_posted",
            count=len(posted_ids),
            thread_url=thread_url,
            tweet_ids=posted_ids,
        )

        return {
            "thread_tweet_ids": posted_ids,
            "thread_url": thread_url,
            "count": len(posted_ids),
            "partial": False,
        }

    def get_schema(self) -> dict:
        """Return JSON schema for thread_x inputs."""
        return {
            "type": "object",
            "properties": {
                "tweets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of tweet texts in order (each max 280 chars, min 2 tweets)",
                },
                "bypass_cap": {
                    "type": "boolean",
                    "default": False,
                    "description": "Skip daily tweet cap check",
                },
            },
            "required": ["tweets"],
        }
