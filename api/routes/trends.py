"""
Trend signal and briefing endpoints.
"""

from fastapi import APIRouter
from sqlalchemy import select

from db.base import async_session_factory
from db.models.briefings import Briefing
from db.models.trend_signals import TrendSignal

router = APIRouter(tags=["intelligence"])


@router.get("/trends")
async def list_trends(limit: int = 20) -> list[dict]:
    """List recent trend signals."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(TrendSignal).order_by(TrendSignal.captured_at.desc()).limit(limit)
        )
        signals = result.scalars().all()

    return [
        {
            "id": str(s.id),
            "contract_id": str(s.contract_id),
            "source": s.source,
            "signal": s.signal[:200],
            "score": s.score,
            "captured_at": s.captured_at.isoformat() if s.captured_at else None,
        }
        for s in signals
    ]


@router.get("/briefings")
async def list_briefings(limit: int = 10) -> list[dict]:
    """List weekly briefings."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Briefing).order_by(Briefing.week_of.desc()).limit(limit)
        )
        briefings = result.scalars().all()

    return [
        {
            "id": str(b.id),
            "contract_id": str(b.contract_id),
            "week_of": b.week_of.isoformat() if b.week_of else None,
            "delivered_at": b.delivered_at.isoformat() if b.delivered_at else None,
        }
        for b in briefings
    ]


@router.get("/briefings/latest")
async def latest_briefings() -> list[dict]:
    """Get the latest briefing per contract."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Briefing).order_by(Briefing.week_of.desc()).limit(10)
        )
        briefings = result.scalars().all()

    # Deduplicate by contract
    seen: set = set()
    latest: list[dict] = []
    for b in briefings:
        if b.contract_id not in seen:
            seen.add(b.contract_id)
            latest.append({
                "id": str(b.id),
                "contract_id": str(b.contract_id),
                "week_of": b.week_of.isoformat() if b.week_of else None,
                "competitor_moves": b.competitor_moves,
                "trend_signals": b.trend_signals,
                "opportunities": b.opportunities,
                "content_ideas": b.content_ideas,
            })

    return latest
