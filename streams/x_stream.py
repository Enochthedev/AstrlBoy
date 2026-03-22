"""
Persistent X API v2 filtered stream listener.

Runs forever as a background task alongside the FastAPI app.
Reconnects automatically with exponential backoff on disconnect.
On each tweet: score relevance, store as TrendSignal, dump to R2.
"""

import asyncio
from uuid import uuid4

import tweepy
from tweepy.asynchronous import AsyncStreamingClient

from core.config import settings
from core.logging import get_logger
from contracts.service import contracts_service
from db.base import async_session_factory
from db.models.trend_signals import TrendSignal
from storage.r2 import r2_client

logger = get_logger("streams.x_stream")


class XFilteredStream(AsyncStreamingClient):
    """Persistent async connection to X API v2 filtered stream.

    Scores incoming tweets against all active contract keywords,
    stores relevant signals, and dumps raw data to R2.
    """

    def __init__(self) -> None:
        super().__init__(
            bearer_token=settings.twitter_bearer_token,
            wait_on_rate_limit=True,
        )
        self._reconnect_delay = 1

    async def on_tweet(self, tweet: tweepy.Tweet) -> None:
        """Process each incoming tweet from the filtered stream.

        Args:
            tweet: The incoming tweet object.
        """
        try:
            text = tweet.text or ""
            contracts = await contracts_service.get_active_contracts()

            for contract in contracts:
                keywords = contract.meta.get("stream_keywords", [])
                # Simple keyword matching — score based on keyword hits
                hits = sum(1 for kw in keywords if kw.lower() in text.lower())
                if hits == 0:
                    continue

                score = min(hits / max(len(keywords), 1) * 10, 10.0)
                signal_id = uuid4()

                async with async_session_factory() as session:
                    signal = TrendSignal(
                        id=signal_id,
                        contract_id=contract.id,
                        source="x_stream",
                        signal=text[:2000],
                        keywords=[kw for kw in keywords if kw.lower() in text.lower()],
                        score=score,
                    )
                    session.add(signal)
                    await session.commit()

                # Dump raw to R2
                try:
                    await r2_client.dump(
                        contract_slug=contract.client_slug,
                        entity_type="trend_signals",
                        entity_id=signal_id,
                        data={
                            "tweet_id": str(tweet.id),
                            "text": text,
                            "score": score,
                            "matched_keywords": [kw for kw in keywords if kw.lower() in text.lower()],
                        },
                    )
                except Exception:
                    pass

        except Exception as exc:
            logger.error("stream_tweet_processing_failed", error=str(exc))

    async def on_errors(self, errors: list) -> None:
        """Handle stream errors."""
        logger.error("stream_errors", errors=str(errors))

    async def on_disconnect(self) -> None:
        """Handle stream disconnect — reconnect with exponential backoff."""
        logger.warning("stream_disconnected", reconnect_delay=self._reconnect_delay)
        await asyncio.sleep(self._reconnect_delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, 300)

    async def on_connect(self) -> None:
        """Handle successful stream connection."""
        logger.info("stream_connected")
        self._reconnect_delay = 1  # Reset backoff on successful connect


async def start_stream() -> XFilteredStream | None:
    """Start the filtered stream as a background task.

    Returns:
        The stream instance, or None if bearer token is not configured.
    """
    if not settings.twitter_bearer_token:
        logger.warning("x_stream_not_configured")
        return None

    stream = XFilteredStream()

    # Update stream rules based on active contracts
    contracts = await contracts_service.get_active_contracts()
    all_keywords: set[str] = set()
    for contract in contracts:
        all_keywords.update(contract.meta.get("stream_keywords", []))

    if all_keywords:
        # Clear existing rules
        existing = stream.get_rules()
        if existing and existing.data:
            stream.delete_rules([r.id for r in existing.data])

        # Add new rules (max 512 chars per rule)
        rule_value = " OR ".join(list(all_keywords)[:25])
        stream.add_rules(tweepy.StreamRule(value=rule_value, tag="astrlboy"))

    # Start filtering (non-blocking)
    stream.filter(tweet_fields=["text", "created_at", "author_id"])

    logger.info("x_stream_started", keyword_count=len(all_keywords))
    return stream
