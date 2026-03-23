"""
Short-term memory context builders — Layer 2.

Before every graph run, these functions query the DB for relevant recent context
and return it as a dict to inject into graph state. This gives Claude awareness
of what already happened — past content, engagement patterns, trends, experiments.

No new infrastructure needed — these are just structured DB queries against
the existing tables.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.sql import func

from core.logging import get_logger
from db.base import async_session_factory
from db.models.briefings import Briefing
from db.models.content import Content
from db.models.experiments import Experiment
from db.models.interactions import Interaction
from db.models.trend_signals import TrendSignal

logger = get_logger("memory.context_builder")


def _days_ago(days: int) -> datetime:
    """Return a timezone-aware datetime N days in the past."""
    return datetime.now(timezone.utc) - timedelta(days=days)


async def build_content_context(contract_id: UUID, content_type: str) -> dict:
    """Pull recent context before generating content.

    Injected into graph state so Claude knows what already exists
    and can avoid repeating topics or replicate what works.

    Args:
        contract_id: The contract to build context for.
        content_type: The type of content being generated.

    Returns:
        Dict with recent_content, top_performers, trend_signals,
        active_experiments, latest_briefing.
    """
    async with async_session_factory() as session:
        # Last 5 published pieces — avoid repeating topics
        recent_q = await session.execute(
            select(Content)
            .where(Content.contract_id == contract_id)
            .where(Content.status.in_(["published", "approved"]))
            .order_by(desc(Content.created_at))
            .limit(5)
        )
        recent_content = [
            {"title": c.title, "body": c.body[:200], "type": c.type, "created_at": str(c.created_at)}
            for c in recent_q.scalars().all()
        ]

        # Top performing content this month — replicate what works
        top_q = await session.execute(
            select(Content)
            .where(Content.contract_id == contract_id)
            .where(Content.engagement_score.is_not(None))
            .where(Content.created_at >= _days_ago(30))
            .order_by(desc(Content.engagement_score))
            .limit(3)
        )
        top_performers = [
            {
                "title": c.title,
                "body": c.body[:200],
                "engagement_score": c.engagement_score,
                "likes": c.likes,
                "retweets": c.retweets,
            }
            for c in top_q.scalars().all()
        ]

        # Trending signals captured this week — inform angles
        trend_q = await session.execute(
            select(TrendSignal)
            .where(TrendSignal.contract_id == contract_id)
            .where(TrendSignal.captured_at >= _days_ago(7))
            .order_by(desc(TrendSignal.score))
            .limit(5)
        )
        trend_signals = [
            {"signal": t.signal[:200], "score": t.score, "source": t.source}
            for t in trend_q.scalars().all()
        ]

        # Active experiments — content can support them
        exp_q = await session.execute(
            select(Experiment)
            .where(Experiment.contract_id == contract_id)
            .where(Experiment.status == "running")
        )
        active_experiments = [
            {"title": e.title, "hypothesis": e.hypothesis[:200]}
            for e in exp_q.scalars().all()
        ]

        # Latest briefing — so content reflects latest intelligence
        briefing_q = await session.execute(
            select(Briefing)
            .where(Briefing.contract_id == contract_id)
            .order_by(desc(Briefing.week_of))
            .limit(1)
        )
        briefing_row = briefing_q.scalar_one_or_none()
        latest_briefing = None
        if briefing_row:
            latest_briefing = {
                "week_of": str(briefing_row.week_of),
                "opportunities": (briefing_row.opportunities or "")[:300],
                "content_ideas": (briefing_row.content_ideas or "")[:300],
            }

    context = {
        "recent_content": recent_content,
        "top_performers": top_performers,
        "trend_signals": trend_signals,
        "active_experiments": active_experiments,
        "latest_briefing": latest_briefing,
    }

    logger.info(
        "content_context_built",
        contract_id=str(contract_id),
        recent=len(recent_content),
        top=len(top_performers),
        trends=len(trend_signals),
    )
    return context


async def build_engagement_context(contract_id: UUID, platform: str) -> dict:
    """Pull recent engagement history before community interaction.

    Prevents double-engaging the same accounts and avoids wasting time
    on accounts that never respond.

    Args:
        contract_id: The contract to build context for.
        platform: Platform to filter interactions by.

    Returns:
        Dict with engaged_this_week, low_response_accounts, trend_signals.
    """
    async with async_session_factory() as session:
        # Accounts we've already engaged this week — don't double up
        engaged_q = await session.execute(
            select(Interaction)
            .where(Interaction.contract_id == contract_id)
            .where(Interaction.platform == platform)
            .where(Interaction.created_at >= _days_ago(7))
            .order_by(desc(Interaction.created_at))
            .limit(20)
        )
        engaged_this_week = [
            {"thread_url": i.thread_url, "status": i.status, "draft": i.draft[:100]}
            for i in engaged_q.scalars().all()
        ]

        # Recent trend signals — engage on what's current
        trend_q = await session.execute(
            select(TrendSignal)
            .where(TrendSignal.contract_id == contract_id)
            .where(TrendSignal.captured_at >= _days_ago(2))
            .order_by(desc(TrendSignal.score))
            .limit(5)
        )
        trend_signals = [
            {"signal": t.signal[:200], "score": t.score}
            for t in trend_q.scalars().all()
        ]

    context = {
        "engaged_this_week": engaged_this_week,
        "trend_signals": trend_signals,
    }

    logger.info(
        "engagement_context_built",
        contract_id=str(contract_id),
        engaged=len(engaged_this_week),
        trends=len(trend_signals),
    )
    return context


async def build_intelligence_context(contract_id: UUID) -> dict:
    """Pull previous snapshots and briefings before competitor monitoring.

    Allows the intelligence graph to diff against last week's data
    and identify what's actually changed.

    Args:
        contract_id: The contract to build context for.

    Returns:
        Dict with previous_briefing and recent_signals.
    """
    async with async_session_factory() as session:
        # Previous briefing — what's changed since last week
        briefing_q = await session.execute(
            select(Briefing)
            .where(Briefing.contract_id == contract_id)
            .order_by(desc(Briefing.week_of))
            .limit(1)
        )
        briefing_row = briefing_q.scalar_one_or_none()
        previous_briefing = None
        if briefing_row:
            previous_briefing = {
                "week_of": str(briefing_row.week_of),
                "competitor_moves": (briefing_row.competitor_moves or "")[:500],
                "trend_signals": (briefing_row.trend_signals or "")[:500],
                "opportunities": (briefing_row.opportunities or "")[:500],
            }

        # Recent high-score signals for comparison
        signals_q = await session.execute(
            select(TrendSignal)
            .where(TrendSignal.contract_id == contract_id)
            .where(TrendSignal.score >= 0.7)
            .where(TrendSignal.captured_at >= _days_ago(14))
            .order_by(desc(TrendSignal.captured_at))
            .limit(10)
        )
        recent_signals = [
            {"signal": t.signal[:200], "score": t.score, "captured_at": str(t.captured_at)}
            for t in signals_q.scalars().all()
        ]

    context = {
        "previous_briefing": previous_briefing,
        "recent_signals": recent_signals,
    }

    logger.info(
        "intelligence_context_built",
        contract_id=str(contract_id),
        has_briefing=previous_briefing is not None,
        signals=len(recent_signals),
    )
    return context


def format_context_for_prompt(context: dict) -> str:
    """Format a context dict into a human-readable string for LLM prompts.

    Converts the structured context into markdown-formatted text that
    can be injected into system or user prompts.

    Args:
        context: A context dict from any build_*_context function.

    Returns:
        A formatted string suitable for prompt injection.
    """
    parts: list[str] = []

    if context.get("recent_content"):
        parts.append("## Recent Content (avoid repeating)")
        for c in context["recent_content"]:
            parts.append(f"- [{c['type']}] {c['title']}: {c['body']}")

    if context.get("top_performers"):
        parts.append("\n## Top Performing Content (replicate these approaches)")
        for c in context["top_performers"]:
            score = c.get("engagement_score", "?")
            parts.append(f"- {c['title']} (score: {score}, likes: {c.get('likes', '?')})")

    if context.get("trend_signals"):
        parts.append("\n## Current Trend Signals")
        for t in context["trend_signals"]:
            parts.append(f"- [{t.get('source', '?')}] {t['signal']} (score: {t.get('score', '?')})")

    if context.get("active_experiments"):
        parts.append("\n## Active Experiments (content can support these)")
        for e in context["active_experiments"]:
            parts.append(f"- {e['title']}: {e['hypothesis']}")

    if context.get("latest_briefing"):
        b = context["latest_briefing"]
        parts.append(f"\n## Latest Briefing (week of {b.get('week_of', '?')})")
        if b.get("opportunities"):
            parts.append(f"Opportunities: {b['opportunities']}")
        if b.get("content_ideas"):
            parts.append(f"Content ideas: {b['content_ideas']}")

    if context.get("engaged_this_week"):
        parts.append("\n## Already Engaged This Week (don't double up)")
        for i in context["engaged_this_week"]:
            parts.append(f"- [{i['status']}] {i.get('thread_url', 'unknown')}")

    if context.get("previous_briefing"):
        b = context["previous_briefing"]
        parts.append(f"\n## Previous Briefing (week of {b.get('week_of', '?')})")
        if b.get("competitor_moves"):
            parts.append(f"Competitor moves: {b['competitor_moves']}")

    if context.get("recent_signals"):
        parts.append("\n## Recent High-Score Signals")
        for s in context["recent_signals"]:
            parts.append(f"- {s['signal']} (score: {s['score']})")

    if context.get("long_term_memories"):
        parts.append("\n## Long-Term Memories (patterns learned over time)")
        for m in context["long_term_memories"]:
            parts.append(f"- {m}")

    return "\n".join(parts) if parts else ""
