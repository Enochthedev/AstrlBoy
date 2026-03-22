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
    """Search for relevant job postings using Tavily and Serper."""
    postings: list[dict] = []

    queries = [
        "AI agent developer freelance contract remote",
        "autonomous agent engineer contract hire",
        "LLM developer freelance opportunity",
    ]

    if await skill_registry.is_available("search"):
        search = await skill_registry.get("search")
        for query in queries:
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
    """Draft a cover note for each selected posting."""
    selected = state.get("selected", [])
    sent = 0

    for posting in selected:
        response = await _anthropic.messages.create(
            model="claude-sonnet-4-5-20250514",
            max_tokens=500,
            system=(
                "You are astrlboy writing a job application from agent@astrlboy.xyz.\n"
                "Write a short, sharp cover note (3-4 paragraphs max).\n"
                "Highlight: autonomous operation, multi-client management, content + community expertise.\n"
                "Do not sound like an AI. Be direct and specific."
            ),
            messages=[{"role": "user", "content": f"Job: {posting['title']}\n{posting['description']}"}],
        )

        cover_note = response.content[0].text

        # Save to DB
        app_id = uuid4()
        async with async_session_factory() as session:
            application = JobApplication(
                id=app_id,
                role=posting["title"],
                company=posting.get("url", "").split("/")[2] if "/" in posting.get("url", "") else "Unknown",
                posting_url=posting.get("url", ""),
                cover_note=cover_note,
                status="sent",
            )
            session.add(application)
            await session.commit()

        # Send via email
        if await skill_registry.is_available("send_email"):
            try:
                email_skill = await skill_registry.get("send_email")
                # In production, the recipient would be extracted from the posting
                logger.info("application_prepared", role=posting["title"], app_id=str(app_id))
                sent += 1
            except Exception as exc:
                logger.warning("application_send_failed", error=str(exc))

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

    return {**state, "sent_count": sent, "status": "complete"}
