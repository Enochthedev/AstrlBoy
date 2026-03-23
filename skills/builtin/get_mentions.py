"""
X mentions skill.

Fetches recent mentions of @astrlboy_ so the agent can read context and reply.
Uses cached identity to avoid paying $0.01 per get_me() call.
"""

from typing import Any

import tweepy

from cache.x_identity import get_x_user_id
from core.budget import XOperation, budget_tracker
from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.get_mentions")


class GetMentionsSkill(BaseTool):
    """Fetch recent mentions from X."""

    name = "get_mentions"
    description = "Get recent tweets mentioning @astrlboy_. Returns tweet text, author, and context."
    version = "1.1.0"

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
        since_id: str | None = None,
        max_results: int = 10,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch recent mentions.

        Args:
            since_id: Only return tweets after this ID (for pagination).
            max_results: Maximum number of mentions to return (5-100).

        Returns:
            List of mention dicts with id, text, author_id, conversation_id.

        Raises:
            SkillExecutionError: If the API call fails.
        """
        try:
            # Use cached identity instead of calling get_me() ($0.01 saved per call)
            user_id = await get_x_user_id()

            response = self._client.get_users_mentions(
                id=user_id,
                since_id=since_id,
                max_results=max(5, min(max_results, 100)),
                tweet_fields=["created_at", "conversation_id", "in_reply_to_user_id", "text"],
                expansions=["author_id"],
            )

            if not response or not response.data:
                return []

            # Track read cost
            if budget_tracker:
                await budget_tracker.track(XOperation.POST_READ, count=len(response.data))

            # Build author lookup
            authors = {}
            if response.includes and "users" in response.includes:
                for user in response.includes["users"]:
                    authors[user.id] = user.username

            mentions = []
            for tweet in response.data:
                mentions.append({
                    "id": str(tweet.id),
                    "text": tweet.text,
                    "author_id": str(tweet.author_id),
                    "author_username": authors.get(tweet.author_id, "unknown"),
                    "conversation_id": str(tweet.conversation_id) if tweet.conversation_id else None,
                    "in_reply_to_user_id": str(tweet.in_reply_to_user_id) if tweet.in_reply_to_user_id else None,
                    "created_at": str(tweet.created_at) if tweet.created_at else None,
                })

            logger.info("mentions_fetched", count=len(mentions))
            return mentions
        except SkillExecutionError:
            raise
        except Exception as exc:
            logger.error("get_mentions_failed", error=str(exc))
            raise SkillExecutionError(f"Get mentions failed: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for get_mentions inputs."""
        return {
            "type": "object",
            "properties": {
                "since_id": {"type": "string", "description": "Only mentions after this tweet ID"},
                "max_results": {"type": "integer", "description": "Max results (5-100)"},
            },
            "required": [],
        }
