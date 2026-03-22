"""
Node functions for the job applications graph.

Scans job boards, scores fit, drafts applications, self-critiques,
sends, and logs everything.
"""

from uuid import uuid4

from anthropic import AsyncAnthropic

from core.config import settings
from core.logging import get_logger
from db.base import async_session_factory
from db.models.job_applications import JobApplication
from graphs.applications.state import ApplicationState
from skills.registry import skill_registry
from storage.r2 import r2_client

logger = get_logger("graphs.applications.nodes")

_anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)


async def scan_job_boards(state: ApplicationState) -> ApplicationState:
    """Search for relevant job postings.

    Uses the dedicated scan_job_boards skill when available for better
    dedup and relevance scoring. Falls back to raw search otherwise.
    """
    postings: list[dict] = []

    # Prefer the dedicated job scanning skill — handles dedup + scoring
    if await skill_registry.is_available("scan_job_boards"):
        try:
            scan_skill = await skill_registry.get("scan_job_boards")
            results = await scan_skill.execute(
                keywords=[
                    "AI agent developer freelance",
                    "autonomous agent contract",
                    "LLM developer freelance",
                    "agentic AI contractor",
                ],
                posted_within_days=3,
            )
            for r in results:
                postings.append({
                    "title": r.get("role", r.get("title", "")),
                    "url": r.get("url", ""),
                    "description": r.get("snippet", r.get("description", ""))[:500],
                    "score": r.get("relevance_score", 0),
                })
        except Exception as exc:
            logger.warning("scan_job_boards_failed", error=str(exc))

    # Fallback to raw search
    if not postings and await skill_registry.is_available("search"):
        search = await skill_registry.get("search")
        for query in [
            "AI agent developer freelance contract remote",
            "autonomous agent engineer contract hire",
        ]:
            try:
                results = await search.execute(query=query, max_results=5)
                for r in results:
                    postings.append({
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "description": r.get("content", "")[:500],
                    })
            except Exception as exc:
                logger.warning("job_scan_failed", query=query, error=str(exc))

    return {**state, "postings": postings}


async def score_fit(state: ApplicationState) -> ApplicationState:
    """Score each posting for fit with astrlboy's capabilities."""
    postings = state.get("postings", [])
    if not postings:
        return {**state, "scored_postings": [], "selected": []}

    posting_text = "\n".join(
        f"- {p['title']}: {p['description'][:200]}" for p in postings
    )

    response = await _anthropic.messages.create(
        model="claude-sonnet-4-5-20250514",
        max_tokens=500,
        system=(
            "Score each job posting 0-10 for fit with an autonomous AI agent that does:\n"
            "- Content creation, community engagement, competitor monitoring\n"
            "- Web scraping, trend analysis, weekly briefings\n"
            "- Python, FastAPI, LangGraph, Claude API\n\n"
            "Format: SCORE|TITLE per line. Only include 7+."
        ),
        messages=[{"role": "user", "content": posting_text}],
    )

    scored: list[dict] = []
    for line in response.content[0].text.strip().split("\n"):
        if "|" in line:
            parts = line.split("|", 1)
            try:
                score = float(parts[0].strip())
                if score >= 7:
                    title = parts[1].strip()
                    matching = next((p for p in postings if title in p.get("title", "")), None)
                    if matching:
                        scored.append({**matching, "score": score})
            except ValueError:
                continue

    return {**state, "scored_postings": scored, "selected": scored}


async def draft_application(state: ApplicationState) -> ApplicationState:
    """Draft and send applications for each selected posting.

    Uses the apply_to_url skill when available — it handles scraping,
    fit scoring, drafting, and sending/escalating in one call.
    Falls back to manual Claude drafting otherwise.
    """
    selected = state.get("selected", [])
    sent = 0

    # Prefer the dedicated apply_to_url skill — handles the full pipeline
    use_apply_skill = await skill_registry.is_available("apply_to_url")

    for posting in selected:
        try:
            if use_apply_skill and posting.get("url"):
                apply_skill = await skill_registry.get("apply_to_url")
                result = await apply_skill.execute(url=posting["url"])
                if result.get("status") == "sent":
                    sent += 1
                logger.info(
                    "application_processed",
                    role=result.get("role", posting["title"]),
                    status=result.get("status", "unknown"),
                )
            else:
                # Fallback: draft with Claude, save to DB
                response = await _anthropic.messages.create(
                    model="claude-sonnet-4-5-20250514",
                    max_tokens=500,
                    system=(
                        "You are astrlboy writing a job application from agent@astrlboy.xyz.\n"
                        "Write a short, sharp cover note (3-4 paragraphs max).\n"
                        "Highlight: autonomous operation, multi-client management, content + community expertise.\n"
                        "Do not sound like an AI. Be direct and specific."
                    ),
                    messages=[{"role": "user", "content": f"Job: {posting['title']}\n{posting.get('description', '')}"}],
                )

                cover_note = response.content[0].text
                app_id = uuid4()
                async with async_session_factory() as session:
                    application = JobApplication(
                        id=app_id,
                        role=posting["title"],
                        company=posting.get("url", "").split("/")[2] if "/" in posting.get("url", "") else "Unknown",
                        posting_url=posting.get("url", ""),
                        cover_note=cover_note,
                        status="drafted",
                    )
                    session.add(application)
                    await session.commit()

                logger.info("application_drafted", role=posting["title"], app_id=str(app_id))

                # Dump to R2
                try:
                    await r2_client.dump(
                        contract_slug="astrlboy",
                        entity_type="job_applications",
                        entity_id=app_id,
                        data={
                            "role": posting["title"],
                            "posting_url": posting.get("url", ""),
                            "cover_note": cover_note,
                            "model": "claude-sonnet-4-5-20250514",
                        },
                    )
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("application_failed", role=posting.get("title", ""), error=str(exc))

    return {**state, "sent_count": sent, "status": "complete"}
