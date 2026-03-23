"""
Weekly memory compression — distills raw interactions into long-term memories.

Runs every Sunday at 18:00 WAT. For each active contract:
1. Pulls all interactions from the past week
2. Asks Claude to extract key learnings as concise facts
3. Stores each learning in mem0 as a long-term memory
4. Raw data older than 30 days stays in R2 for training

This prevents memory bloat while preserving the patterns that matter.
"""

import json
from uuid import UUID

from core.ai import create_message
from core.logging import get_logger
from memory.mem0_client import agent_memory

logger = get_logger("memory.compression")


async def compress_weekly_memories(
    contract_id: UUID,
    contract_slug: str,
) -> int:
    """Compress the week's raw interactions into concise long-term memories.

    Pulls recent content, interactions, and trend signals, then asks Claude
    to extract the most important learnings as self-contained facts.

    Args:
        contract_id: The contract to compress memories for.
        contract_slug: Slug for scoping mem0 storage.

    Returns:
        Number of learnings stored.
    """
    if not agent_memory.available:
        logger.warning("compression_skipped_no_mem0", contract=contract_slug)
        return 0

    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select
    from sqlalchemy.sql import desc

    from db.base import async_session_factory
    from db.models.content import Content
    from db.models.interactions import Interaction
    from db.models.trend_signals import TrendSignal

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)

    async with async_session_factory() as session:
        # Pull published content from the past week
        content_q = await session.execute(
            select(Content)
            .where(Content.contract_id == contract_id)
            .where(Content.created_at >= week_ago)
            .order_by(desc(Content.created_at))
            .limit(20)
        )
        content_rows = content_q.scalars().all()

        # Pull interactions from the past week
        interactions_q = await session.execute(
            select(Interaction)
            .where(Interaction.contract_id == contract_id)
            .where(Interaction.created_at >= week_ago)
            .order_by(desc(Interaction.created_at))
            .limit(30)
        )
        interaction_rows = interactions_q.scalars().all()

        # Pull trend signals from the past week
        signals_q = await session.execute(
            select(TrendSignal)
            .where(TrendSignal.contract_id == contract_id)
            .where(TrendSignal.captured_at >= week_ago)
            .order_by(desc(TrendSignal.score))
            .limit(20)
        )
        signal_rows = signals_q.scalars().all()

    # Build summary for Claude
    summary_parts: list[str] = []

    if content_rows:
        summary_parts.append(f"CONTENT ({len(content_rows)} pieces):")
        for c in content_rows:
            metrics = ""
            if c.engagement_score:
                metrics = f" | score={c.engagement_score}, likes={c.likes}, RTs={c.retweets}"
            summary_parts.append(
                f"  - [{c.type}] {c.title}: {c.body[:150]}... (status: {c.status}{metrics})"
            )

    if interaction_rows:
        summary_parts.append(f"\nINTERACTIONS ({len(interaction_rows)} total):")
        for i in interaction_rows:
            summary_parts.append(
                f"  - [{i.platform}/{i.status}] {i.draft[:100]}..."
            )

    if signal_rows:
        summary_parts.append(f"\nTREND SIGNALS ({len(signal_rows)} captured):")
        for s in signal_rows:
            summary_parts.append(
                f"  - [{s.source}] {s.signal[:100]}... (score: {s.score})"
            )

    if not summary_parts:
        logger.info("compression_no_data", contract=contract_slug)
        return 0

    raw_summary = "\n".join(summary_parts)

    # Ask Claude to extract key learnings
    response = await create_message(
        model="claude-haiku-4-5",
        max_tokens=1500,
        system=(
            "You extract key learnings from an AI agent's weekly activity log. "
            "Return a JSON array of 5-10 concise facts (strings). Each fact should be "
            "a self-contained statement under 100 words. Focus on:\n"
            "- What content angles worked or didn't\n"
            "- Which accounts engaged or ignored us\n"
            "- Patterns worth remembering long-term\n"
            "- Competitor or market signals\n"
            "- Any other actionable intelligence\n\n"
            "Return ONLY a JSON array of strings. No other text."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Review these {len(content_rows) + len(interaction_rows)} activities "
                f"from the past week for {contract_slug}:\n\n{raw_summary}"
            ),
        }],
    )

    # Parse learnings from Claude's response
    text = response.content[0].text.strip()

    # Handle potential markdown code fences
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        learnings = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("compression_parse_failed", contract=contract_slug, raw=text[:200])
        return 0

    if not isinstance(learnings, list):
        logger.warning("compression_invalid_format", contract=contract_slug)
        return 0

    # Store each learning as a long-term memory
    stored = 0
    for learning in learnings:
        if isinstance(learning, str) and learning.strip():
            try:
                await agent_memory.add(
                    content=learning.strip(),
                    contract_slug=contract_slug,
                    category="weekly_summary",
                )
                stored += 1
            except Exception as exc:
                logger.warning("memory_store_failed", error=str(exc))

    logger.info(
        "memory_compressed",
        contract=contract_slug,
        learnings_stored=stored,
        content_processed=len(content_rows),
        interactions_processed=len(interaction_rows),
        signals_processed=len(signal_rows),
    )

    return stored
