"""
X (Twitter) reply skill.

Replies to a specific tweet on X. Unlike post_x, this skill reads the full
thread context before replying — never reply blind. The thread context helps
LangGraph nodes draft better replies and decide whether to engage at all.

Replies count toward the monthly tweet limit, so engagement graphs should
score threads before routing here. If replying to a high-follower account,
the self-critique threshold should be raised to 9/10 in the engagement graph.
"""

import asyncio
from typing import Any

import tweepy

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.reply_x")

# Retry config — 3 attempts with exponential backoff (1s, 2s, 4s)
_MAX_RETRIES = 3
_BASE_DELAY = 1.0


class ReplyXSkill(BaseTool):
    """Reply to a tweet on X, reading thread context first.

    This skill does two things in sequence:
    1. Fetches the original tweet (and its conversation thread if available)
       so calling code has full context for drafting.
    2. Posts the reply using create_tweet with in_reply_to_tweet_id.

    The thread context is returned alongside the posted reply so that
    graphs can log the full interaction for training data.
    """

    name = "reply_x"
    description = (
        "Reply to a specific tweet on X. Reads thread context first, "
        "then posts the reply. Returns both the thread context and the posted reply."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        # OAuth 1.0a client for posting (user context)
        self._client = tweepy.Client(
            consumer_key=settings.twitter_api_key,
            consumer_secret=settings.twitter_api_secret,
            access_token=settings.twitter_access_token,
            access_token_secret=settings.twitter_access_secret,
        )

    async def _fetch_thread_context(self, tweet_id: str) -> dict[str, Any]:
        """Fetch the original tweet and available thread context.

        Retrieves the target tweet with author info and public metrics.
        If the tweet is part of a conversation, fetches additional tweets
        in the same conversation thread for context.

        Args:
            tweet_id: The ID of the tweet to fetch context for.

        Returns:
            Dict with 'original_tweet', 'author', and 'thread' (list of
            preceding tweets in the conversation).

        Raises:
            SkillExecutionError: If fetching the tweet fails after retries.
        """
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                # Fetch the target tweet with expansions
                tweet_response = self._client.get_tweet(
                    tweet_id,
                    tweet_fields=["conversation_id", "created_at", "public_metrics", "author_id"],
                    user_fields=["name", "username", "public_metrics"],
                    expansions=["author_id"],
                )

                if not tweet_response.data:
                    raise SkillExecutionError(
                        f"Tweet {tweet_id} not found or not accessible"
                    )

                tweet_data = tweet_response.data
                conversation_id = tweet_data.conversation_id

                # Extract author info from includes
                author_info: dict[str, Any] = {}
                if tweet_response.includes and "users" in tweet_response.includes:
                    user = tweet_response.includes["users"][0]
                    author_info = {
                        "name": user.name,
                        "username": user.username,
                        "followers_count": getattr(
                            user, "public_metrics", {}
                        ).get("followers_count", 0)
                        if hasattr(user, "public_metrics") and user.public_metrics
                        else 0,
                    }

                # Fetch conversation thread for context — only if this tweet
                # is part of a larger conversation (not a standalone tweet)
                thread_tweets: list[dict[str, Any]] = []
                if conversation_id and str(conversation_id) != str(tweet_id):
                    try:
                        search_response = self._client.search_recent_tweets(
                            query=f"conversation_id:{conversation_id}",
                            tweet_fields=["created_at", "author_id", "in_reply_to_user_id"],
                            max_results=10,
                        )
                        if search_response.data:
                            thread_tweets = [
                                {
                                    "id": str(t.id),
                                    "text": t.text,
                                    "created_at": str(t.created_at) if t.created_at else None,
                                }
                                for t in search_response.data
                            ]
                    except Exception as thread_exc:
                        # Thread fetch is best-effort — don't fail the whole operation
                        logger.warning(
                            "thread_fetch_partial",
                            tweet_id=tweet_id,
                            conversation_id=str(conversation_id),
                            error=str(thread_exc),
                        )

                context = {
                    "original_tweet": {
                        "id": str(tweet_data.id),
                        "text": tweet_data.text,
                        "conversation_id": str(conversation_id) if conversation_id else None,
                        "created_at": str(tweet_data.created_at) if tweet_data.created_at else None,
                        "public_metrics": dict(tweet_data.public_metrics)
                        if tweet_data.public_metrics
                        else {},
                    },
                    "author": author_info,
                    "thread": thread_tweets,
                }

                logger.info(
                    "thread_context_fetched",
                    tweet_id=tweet_id,
                    author=author_info.get("username", "unknown"),
                    thread_length=len(thread_tweets),
                )
                return context

            except SkillExecutionError:
                raise
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "thread_context_retry",
                    tweet_id=tweet_id,
                    attempt=attempt,
                    max_retries=_MAX_RETRIES,
                    error=str(exc),
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_BASE_DELAY * (2 ** (attempt - 1)))

        logger.error("thread_context_failed", tweet_id=tweet_id, error=str(last_exc))
        raise SkillExecutionError(
            f"Failed to fetch thread context for tweet {tweet_id} "
            f"after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    async def execute(
        self,
        tweet_id: str,
        text: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Reply to a tweet on X.

        Fetches the original tweet and thread context first, then posts
        the reply. Returns both the thread context and the reply details
        so graphs can log the full interaction to R2 for training data.

        Args:
            tweet_id: ID of the tweet to reply to.
            text: Reply text (max 280 characters).

        Returns:
            Dict with 'reply_tweet_id', 'url', 'text', and 'thread_context'.

        Raises:
            SkillExecutionError: If fetching context or posting the reply fails.
        """
        # Step 1: Read the thread context — never reply blind
        thread_context = await self._fetch_thread_context(tweet_id)

        # Step 2: Post the reply with retries
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = self._client.create_tweet(
                    text=text,
                    in_reply_to_tweet_id=tweet_id,
                )
                reply_id = response.data["id"]

                # Build the reply URL using the author's handle if available
                author_username = thread_context.get("author", {}).get("username", "i")
                url = f"https://x.com/{author_username}/status/{reply_id}"

                logger.info(
                    "reply_posted",
                    reply_tweet_id=reply_id,
                    in_reply_to=tweet_id,
                    author=thread_context.get("author", {}).get("username", "unknown"),
                    url=url,
                )

                return {
                    "reply_tweet_id": reply_id,
                    "url": url,
                    "text": text,
                    "thread_context": thread_context,
                }

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "reply_x_retry",
                    tweet_id=tweet_id,
                    attempt=attempt,
                    max_retries=_MAX_RETRIES,
                    error=str(exc),
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_BASE_DELAY * (2 ** (attempt - 1)))

        logger.error(
            "reply_x_failed",
            tweet_id=tweet_id,
            error=str(last_exc),
        )
        raise SkillExecutionError(
            f"Reply to tweet {tweet_id} failed after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    def get_schema(self) -> dict:
        """Return JSON schema for reply_x inputs."""
        return {
            "type": "object",
            "properties": {
                "tweet_id": {
                    "type": "string",
                    "description": "ID of the tweet to reply to",
                },
                "text": {
                    "type": "string",
                    "description": "Reply text (max 280 characters)",
                },
            },
            "required": ["tweet_id", "text"],
        }
