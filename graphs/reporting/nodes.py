"""
Node functions for the weekly briefing graph.

Aggregates the week's intelligence data and synthesizes it into
a structured briefing delivered via email.
"""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from anthropic import AsyncAnthropic
from sqlalchemy import select

from core.config import settings
from core.logging import get_logger
from db.base import async_session_factory
from db.models.briefings import Briefing
from db.models.trend_signals import TrendSignal
from graphs.reporting.state import ReportingState
from skills.registry import skill_registry
from storage.r2 import r2_client

logger = get_logger("graphs.reporting.nodes")

_anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)


async def aggregate_intelligence(state: ReportingState) -> ReportingState:
    """Pull the week's trend signals and intelligence from the DB."""
    week_start = datetime.now(timezone.utc) - timedelta(days=7)

    async with async_session_factory() as session:
        result = await session.execute(
            select(TrendSignal)
            .where(TrendSignal.contract_id == state["contract_id"])
            .where(TrendSignal.captured_at >= week_start)
            .order_by(TrendSignal.score.desc())
            .limit(20)
        )
        signals = result.scalars().all()

    trend_text = "\n".join(
        f"- [{s.score:.1f}] {s.signal[:200]}" for s in signals
    )

    return {**state, "trend_signals": trend_text or "No trend signals this week."}


async def synthesize(state: ReportingState) -> ReportingState:
    """Synthesize intelligence into a structured weekly briefing."""
    meta = state["contract_meta"]

    response = await _anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=(
            f"You are astrlboy preparing a weekly intelligence briefing for {meta.get('description', 'a client')}.\n\n"
            "Structure:\n"
            "1. Competitor Moves — what competitors did this week\n"
            "2. Trend Signals — what's happening in the market\n"
            "3. Opportunities — specific, actionable things the client should do\n"
            "4. Content Ideas — 3-5 content pieces based on this intelligence\n\n"
            "Be sharp, specific, and opinionated. No filler."
        ),
        messages=[{"role": "user", "content": f"Trend signals:\n{state.get('trend_signals', 'None available')}"}],
    )

    briefing_text = response.content[0].text

    # Parse sections
    sections = {"competitor_moves": "", "opportunities": "", "content_ideas": ""}
    current = ""
    for line in briefing_text.split("\n"):
        lower = line.lower()
        if "competitor" in lower:
            current = "competitor_moves"
        elif "opportunit" in lower:
            current = "opportunities"
        elif "content idea" in lower:
            current = "content_ideas"
        elif current:
            sections[current] += line + "\n"

    return {
        **state,
        "briefing": briefing_text,
        "competitor_moves": sections["competitor_moves"].strip(),
        "opportunities": sections["opportunities"].strip(),
        "content_ideas": sections["content_ideas"].strip(),
    }


async def deliver(state: ReportingState) -> ReportingState:
    """Email the briefing to the configured recipients."""
    meta = state["contract_meta"]
    recipients = meta.get("briefing_recipients", [])

    if recipients and await skill_registry.is_available("send_email"):
        email_skill = await skill_registry.get("send_email")
        for recipient in recipients:
            try:
                await email_skill.execute(
                    to=recipient,
                    subject=f"Weekly Briefing — {state['contract_slug']} — {state.get('week_of', date.today())}",
                    body=state.get("briefing", ""),
                )
            except Exception as exc:
                logger.warning("briefing_delivery_failed", to=recipient, error=str(exc))

    return state


async def store(state: ReportingState) -> ReportingState:
    """Persist the briefing to the DB and R2."""
    briefing_id = uuid4()

    async with async_session_factory() as session:
        briefing = Briefing(
            id=briefing_id,
            contract_id=state["contract_id"],
            week_of=state.get("week_of", date.today()),
            competitor_moves=state.get("competitor_moves", ""),
            trend_signals=state.get("trend_signals", ""),
            opportunities=state.get("opportunities", ""),
            content_ideas=state.get("content_ideas", ""),
            delivered_at=datetime.now(timezone.utc),
        )
        session.add(briefing)
        await session.commit()

    try:
        await r2_client.dump(
            contract_slug=state["contract_slug"],
            entity_type="briefings",
            entity_id=briefing_id,
            data={"briefing": state.get("briefing", ""), "model": "claude-sonnet-4-6"},
        )
    except Exception:
        pass

    return {**state, "briefing_id": briefing_id, "status": "delivered"}
