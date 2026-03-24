"""
X (Twitter) posting skill.

Posts tweets via OAuth 1.0a. Used for autonomous content publishing
and community engagement on X.

Every tweet goes through the budget system before posting:
1. Check daily tweet cap (hard limit)
2. Check monthly budget (soft warning)
3. Track the cost after posting

Media support: pass media_url to download and attach an image/GIF to the tweet.
Uses the v1.1 media upload endpoint alongside the v2 tweet creation endpoint.
"""

import asyncio
import os
import tempfile
from typing import Any

import httpx
import tweepy

from core.budget import XOperation, budget_tracker
from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.post_x")


class PostXSkill(BaseTool):
    """Post a tweet to X via OAuth 1.0a.

    Enforces daily tweet cap and tracks cost against the monthly budget.
    All tweets (original + replies) count toward the same daily cap.
    """

    name = "post_x"
    description = "Post a tweet to X (Twitter). Supports text tweets and replies."
    version = "1.1.0"

    def __init__(self) -> None:
        self._client = tweepy.Client(
            bearer_token=settings.twitter_bearer_token,
            consumer_key=settings.twitter_api_key,
            consumer_secret=settings.twitter_api_secret,
            access_token=settings.twitter_access_token,
            access_token_secret=settings.twitter_access_secret,
        )
        # v1.1 API — used only for media upload (no v2 equivalent exists)
        _auth = tweepy.OAuth1UserHandler(
            settings.twitter_api_key,
            settings.twitter_api_secret,
            settings.twitter_access_token,
            settings.twitter_access_secret,
        )
        self._api_v1 = tweepy.API(_auth)

    async def _upload_media(self, media_url: str) -> str | None:
        """Download media from URL and upload to X. Returns media_id string.

        Uses v1.1 media upload endpoint — no v2 equivalent exists.
        Runs the blocking tweepy call in a thread executor to stay async.
        """
        tmppath = None
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(media_url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "image/jpeg")

            ext = ".gif" if "gif" in content_type else (".png" if "png" in content_type else ".jpg")
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                f.write(resp.content)
                tmppath = f.name

            loop = asyncio.get_event_loop()
            media = await loop.run_in_executor(
                None, lambda: self._api_v1.media_upload(filename=tmppath)
            )
            return str(media.media_id)
        except Exception as exc:
            logger.warning("media_upload_failed", url=media_url[:80], error=str(exc))
            return None
        finally:
            if tmppath:
                try:
                    os.unlink(tmppath)
                except OSError:
                    pass

    async def execute(
        self,
        text: str,
        reply_to_id: str | None = None,
        quote_tweet_id: str | None = None,
        media_url: str | None = None,
        bypass_cap: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Post a tweet.

        Args:
            text: The tweet text (max 280 characters).
            reply_to_id: Optional tweet ID to reply to.
            quote_tweet_id: Optional tweet ID to quote.
            media_url: Optional URL of an image or GIF to attach (downloaded + uploaded automatically).
            bypass_cap: If True, skip the daily tweet cap check.
                Only used for replies to our own tweets.

        Returns:
            Dict with 'tweet_id' and 'text' of the posted tweet.

        Raises:
            SkillExecutionError: If posting fails or daily cap is reached.
        """
        # Enforce daily tweet cap unless bypassed (own-tweet replies)
        if not bypass_cap and budget_tracker:
            can_tweet = await budget_tracker.check_tweet_budget()
            if not can_tweet:
                count = await budget_tracker.get_tweet_count_today()
                logger.warning(
                    "daily_tweet_cap_reached",
                    count=count,
                    cap=budget_tracker.daily_tweet_cap,
                )
                raise SkillExecutionError(
                    f"Daily tweet cap reached ({count}/{budget_tracker.daily_tweet_cap}). "
                    "Skipping to save budget."
                )

        # Upload media if provided
        media_ids = None
        if media_url:
            media_id = await self._upload_media(media_url)
            if media_id:
                media_ids = [media_id]
                logger.info("media_attached", media_id=media_id)

        try:
            response = self._client.create_tweet(
                text=text,
                in_reply_to_tweet_id=reply_to_id,
                quote_tweet_id=quote_tweet_id,
                media_ids=media_ids,
            )
            tweet_id = response.data["id"]

            # Track cost and increment daily counter
            if budget_tracker:
                await budget_tracker.increment_tweet_count()
                await budget_tracker.track(XOperation.POST_CREATE)

            count = await budget_tracker.get_tweet_count_today() if budget_tracker else 0
            logger.info(
                "tweet_posted",
                tweet_id=tweet_id,
                is_reply=bool(reply_to_id),
                has_media=bool(media_ids),
                daily_count=count,
            )
            return {"tweet_id": tweet_id, "text": text}
        except SkillExecutionError:
            raise
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
                "media_url": {"type": "string", "description": "URL of image or GIF to attach (from find_gif or generate_meme)"},
            },
            "required": ["text"],
        }
