"""
Node functions for the intelligence graph.

Scrapes competitors, diffs against previous snapshots, searches for trends,
scores signals, stores them, and identifies opportunities.
"""

import time
from uuid import uuid4

from anthropic import AsyncAnthropic

from core.config import settings
from core.logging import get_logger
from agent.service import agent_service
from db.base import async_session_factory
from db.models.trend_signals import TrendSignal
from graphs.intelligence.state import IntelligenceState
from skills.registry import skill_registry
from storage.r2 import r2_client

logger = get_logger("graphs.intelligence.nodes")

_anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)


async def scrape_competitors(state: IntelligenceState) -> IntelligenceState:
    """Scrape each competitor website listed in the contract meta."""
    meta = state["contract_meta"]
    competitors = meta.get("competitors", [])
    snapshots: list[dict] = []

    if await skill_registry.is_available("scrape"):
        scrape = await skill_registry.get("scrape")
        for competitor in competitors:
            try:
                url = competitor if competitor.startswith("http") else f"https://{competitor}"
                content = await scrape.execute(url=url)
                snapshots.append({"url": url, "content": content[:3000]})
            except Exception as exc:
                logger.warning("competitor_scrape_failed", url=competitor, error=str(exc))

    return {**state, "competitor_snapshots": snapshots}


async def diff_snapshots(state: IntelligenceState) -> IntelligenceState:
    """Compare current competitor snapshots against previous week's data."""
    snapshots = state.get("competitor_snapshots", [])
    if not snapshots:
        return {**state, "diff_from_last_week": "No competitor data available."}

    snapshot_summary = "\n\n".join(
        f"**{s['url']}:**\n{s['content'][:1000]}" for s in snapshots
    )

    response = await _anthropic.messages.create(
        model="claude-sonnet-4-5-20250514",
        max_tokens=1000,
        system="Summarize what these competitors are currently doing. Note any new features, messaging changes, or positioning shifts.",
        messages=[{"role": "user", "content": snapshot_summary}],
    )

    return {**state, "diff_from_last_week": response.content[0].text}


async def search_trends(state: IntelligenceState) -> IntelligenceState:
    """Search for trends using the contract's stream keywords."""
    meta = state["contract_meta"]
    keywords = meta.get("stream_keywords", [])
    signals: list[dict] = []

    if await skill_registry.is_available("search") and keywords:
        search = await skill_registry.get("search")
        query = " ".join(keywords[:5])
        try:
            results = await search.execute(query=query, max_results=10)
            for r in results:
                signals.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                })
        except Exception as exc:
            logger.warning("trend_search_failed", error=str(exc))

    return {**state, "trend_signals": signals}


async def score_signals(state: IntelligenceState) -> IntelligenceState:
    """Score each trend signal for relevance to the client."""
    signals = state.get("trend_signals", [])
    if not signals:
        return {**state, "scored_signals": []}

    meta = state["contract_meta"]
    signal_text = "\n".join(f"- {s['title']}: {s['content'][:200]}" for s in signals)

    response = await _anthropic.messages.create(
        model="claude-sonnet-4-5-20250514",
        max_tokens=1000,
        system=(
            f"Score each signal 0-10 for relevance to: {meta.get('description', 'the client')}.\n"
            "Respond with one line per signal: SCORE|TITLE\n"
            "Drop anything below 5."
        ),
        messages=[{"role": "user", "content": signal_text}],
    )

    scored: list[dict] = []
    for line in response.content[0].text.strip().split("\n"):
        if "|" in line:
            parts = line.split("|", 1)
            try:
                score = float(parts[0].strip())
                if score >= 5:
                    title = parts[1].strip()
                    # Find matching signal
                    matching = next((s for s in signals if title in s.get("title", "")), None)
                    scored.append({**(matching or {"title": title}), "score": score})
            except ValueError:
                continue

    return {**state, "scored_signals": scored}


async def store_signals(state: IntelligenceState) -> IntelligenceState:
    """Persist scored signals to the DB and R2."""
    scored = state.get("scored_signals", [])

    async with async_session_factory() as session:
        for signal_data in scored:
            signal = TrendSignal(
                contract_id=state["contract_id"],
                source="tavily",
                signal=signal_data.get("content", signal_data.get("title", "")),
                keywords=state["contract_meta"].get("stream_keywords", []),
                score=signal_data.get("score", 0.0),
            )
            session.add(signal)
        await session.commit()

    logger.info("signals_stored", count=len(scored), contract_slug=state["contract_slug"])
    return state


async def identify_opportunities(state: IntelligenceState) -> IntelligenceState:
    """Synthesize signals and competitor data into actionable opportunities."""
    scored = state.get("scored_signals", [])
    diff = state.get("diff_from_last_week", "")

    context = (
        f"Competitor analysis:\n{diff}\n\n"
        f"Trend signals:\n" +
        "\n".join(f"- [{s.get('score', 0)}] {s.get('title', '')}" for s in scored)
    )

    response = await _anthropic.messages.create(
        model="claude-sonnet-4-5-20250514",
        max_tokens=1000,
        system="Based on this intelligence, identify 3-5 actionable opportunities. Be specific and opinionated.",
        messages=[{"role": "user", "content": context}],
    )

    return {**state, "opportunities": response.content[0].text, "status": "complete"}
