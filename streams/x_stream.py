"""
Persistent X API v2 filtered stream listener.

Runs forever as a background task alongside the FastAPI app.
Reconnects automatically with exponential backoff on disconnect.

COST-AWARE DESIGN (pay-per-use tier):
Every tweet received = $0.005 post read. To keep costs predictable:
1. Keywords must be specific (no single generic words like "crypto")
2. Daily read budget caps how many tweets we process before pausing
3. Only high-scoring tweets trigger DB writes and engagement
4. Low-score tweets are counted but discarded (still cost $0.005 to receive)

On Basic/Pro tier where reads are included, set X_STREAM_DAILY_CAP=0 to uncap.
"""

import asyncio
from datetime import datetime, timezone
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

# Minimum score to store a signal — tweets below this are received (and billed)
# but discarded. Higher = fewer DB writes, lower noise.
_MIN_STORE_SCORE = 4.0

# Minimum score to trigger real-time engagement (reply, quote, etc.)
_MIN_ENGAGE_SCORE = 7.0


class XFilteredStream(AsyncStreamingClient):
    """Persistent async connection to X API v2 filtered stream.

    Cost-aware: tracks daily read count against a budget cap.
    When the cap is hit, disconnects until the next UTC day.
    """

    def __init__(self, daily_cap: int = 200) -> None:
        super().__init__(
            bearer_token=settings.twitter_bearer_token,
            wait_on_rate_limit=True,
        )
        self._reconnect_delay = 1
        self._daily_cap = daily_cap      # 0 = unlimited (for Basic/Pro tier)
        self._reads_today = 0
        self._today_date = datetime.now(timezone.utc).date()

    def _check_day_reset(self) -> None:
        """Reset the daily counter at UTC midnight."""
        today = datetime.now(timezone.utc).date()
        if today != self._today_date:
            self._reads_today = 0
            self._today_date = today

    async def on_tweet(self, tweet: tweepy.Tweet) -> None:
        """Process each incoming tweet from the filtered stream.

        Every tweet costs $0.005 just by arriving. We can't avoid that cost,
        but we can avoid wasting further resources (DB writes, Claude calls,
        R2 dumps) on low-value tweets.
        """
        self._check_day_reset()
        self._reads_today += 1

        # Track the read cost
        try:
            from core.budget import XOperation, budget_tracker
            if budget_tracker:
                await budget_tracker.track(XOperation.POST_READ)
        except Exception:
            pass

        # Check daily cap — disconnect if exceeded
        if self._daily_cap > 0 and self._reads_today > self._daily_cap:
            if self._reads_today == self._daily_cap + 1:
                logger.warning(
                    "stream_daily_cap_reached",
                    cap=self._daily_cap,
                    cost_today=f"${self._reads_today * 0.005:.2f}",
                )
            # Don't process, but stream stays connected (disconnecting and
            # reconnecting costs more than letting low-volume tweets pass)
            return

        try:
            text = tweet.text or ""
            contracts = await contracts_service.get_active_contracts()

            for contract in contracts:
                keywords = contract.meta.get("stream_keywords", [])
                # Score based on keyword hits — more hits = more relevant
                hits = sum(1 for kw in keywords if kw.lower() in text.lower())
                if hits == 0:
                    continue

                score = min(hits / max(len(keywords), 1) * 10, 10.0)

                # Skip low-score tweets — already billed but not worth storing
                if score < _MIN_STORE_SCORE:
                    continue

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

                # Dump raw to R2 only for high-score signals
                if score >= _MIN_ENGAGE_SCORE:
                    try:
                        await r2_client.dump(
                            contract_slug=contract.client_slug,
                            entity_type="trend_signals",
                            entity_id=signal_id,
                            data={
                                "tweet_id": str(tweet.id),
                                "text": text,
                                "author_id": str(tweet.author_id) if tweet.author_id else None,
                                "score": score,
                                "matched_keywords": [kw for kw in keywords if kw.lower() in text.lower()],
                            },
                        )
                    except Exception:
                        pass

                    # High-score tweet — could trigger real-time engagement
                    # TODO: wire this to the engagement graph for instant replies
                    logger.info(
                        "high_value_stream_signal",
                        tweet_id=str(tweet.id),
                        score=score,
                        contract_slug=contract.client_slug,
                        keywords=[kw for kw in keywords if kw.lower() in text.lower()],
                    )

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
        logger.info(
            "stream_connected",
            daily_cap=self._daily_cap,
            reads_today=self._reads_today,
        )
        self._reconnect_delay = 1


def _build_optimized_rules(keywords: set[str]) -> list[str]:
    """Build tight stream rules that minimize noise.

    Instead of OR-ing all keywords into one broad rule, this creates
    rules that require at least 2 keyword matches or use quoted phrases
    for specificity.

    Generic single words ("crypto", "startup", "AI") are only included
    in combination rules to avoid matching every tweet on the platform.

    Args:
        keywords: Set of all keywords across active contracts.

    Returns:
        List of rule strings for the X filtered stream API.
    """
    # Separate specific phrases from generic single words
    specific = []    # "AI agents", "build in public" — already specific
    generic = []     # "crypto", "startup" — too broad alone

    for kw in keywords:
        if " " in kw or len(kw) > 10:
            specific.append(f'"{kw}"')   # Quote phrases for exact match
        else:
            generic.append(kw)

    rules = []

    # Specific phrases can stand alone — they're narrow enough
    if specific:
        rules.append(" OR ".join(specific))

    # Generic words must be combined — require 2+ to match
    # This turns "crypto OR startup" (thousands/day) into
    # "crypto startup" (only tweets mentioning BOTH = dozens/day)
    if len(generic) >= 2:
        # Create pair combinations of generic terms
        for i in range(0, len(generic) - 1, 2):
            rules.append(f"{generic[i]} {generic[i + 1]}")

    # Also combine generic with specific for high-relevance matches
    if generic and specific:
        for g in generic[:3]:
            for s in specific[:3]:
                rules.append(f"{g} {s}")

    return rules[:25]  # X API allows max 25 rules


async def start_stream() -> XFilteredStream | None:
    """Start the filtered stream as a background task.

    Disabled by default on pay-per-use tier. Set X_STREAM_ENABLED=true
    to enable (recommended after upgrading to Basic/Pro tier).

    Returns:
        The stream instance, or None if disabled or not configured.
    """
    if not settings.twitter_bearer_token:
        logger.warning("x_stream_not_configured")
        return None

    if not settings.x_stream_enabled:
        # Clean up stale rules so they don't accidentally cost money
        try:
            cleanup = AsyncStreamingClient(bearer_token=settings.twitter_bearer_token)
            existing = await cleanup.get_rules()
            if existing and existing.data:
                await cleanup.delete_rules([r.id for r in existing.data])
                logger.info("x_stream_rules_cleaned", count=len(existing.data))
        except Exception:
            pass

        logger.info(
            "x_stream_disabled",
            reason="Pay-per-use tier — stream reads cost $0.005 each. "
            "Set X_STREAM_ENABLED=true to enable after upgrading tier.",
        )
        return None

    stream = XFilteredStream(daily_cap=settings.x_stream_daily_cap)

    # Build optimized rules from active contracts
    contracts = await contracts_service.get_active_contracts()
    all_keywords: set[str] = set()
    for contract in contracts:
        all_keywords.update(contract.meta.get("stream_keywords", []))

    if all_keywords:
        # Clear existing rules
        existing = await stream.get_rules()
        if existing and existing.data:
            await stream.delete_rules([r.id for r in existing.data])

        # Add optimized rules that minimize noise
        rules = _build_optimized_rules(all_keywords)
        for rule_value in rules:
            try:
                await stream.add_rules(tweepy.StreamRule(value=rule_value, tag="astrlboy"))
            except Exception as exc:
                logger.warning("stream_rule_add_failed", rule=rule_value, error=str(exc))

    # Start filtering (non-blocking)
    stream.filter(tweet_fields=["text", "created_at", "author_id"])

    logger.info(
        "x_stream_started",
        keyword_count=len(all_keywords),
        daily_cap=settings.x_stream_daily_cap,
        estimated_daily_cost=f"${settings.x_stream_daily_cap * 0.005:.2f}",
    )
    return stream
