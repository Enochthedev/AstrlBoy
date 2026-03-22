"""
Node functions for the product feedback graph.

Observes friction, user sentiment, and competitor gaps to generate
structured feature requests for clients.
"""

from uuid import uuid4

from anthropic import AsyncAnthropic

from core.config import settings
from core.logging import get_logger
from db.base import async_session_factory
from db.models.feature_requests import FeatureRequest
from graphs.feedback.state import FeedbackState
from skills.registry import skill_registry
from storage.r2 import r2_client

logger = get_logger("graphs.feedback.nodes")

_anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)


async def gather_observations(state: FeedbackState) -> FeedbackState:
    """Gather observations about the client's product from community signals and competitor analysis."""
    meta = state["contract_meta"]
    observations: list[dict] = []

    if await skill_registry.is_available("search"):
        search = await skill_registry.get("search")
        try:
            # Search for user complaints and feature requests
            results = await search.execute(
                query=f"{meta.get('website', '')} feature request OR complaint OR missing OR needs",
                max_results=10,
            )
            for r in results:
                observations.append({
                    "source": r.get("url", ""),
                    "content": r.get("content", "")[:500],
                })
        except Exception as exc:
            logger.warning("observation_search_failed", error=str(exc))

    return {**state, "observations": observations}


async def generate_feature_requests(state: FeedbackState) -> FeedbackState:
    """Synthesize observations into structured feature requests."""
    meta = state["contract_meta"]
    observations = state.get("observations", [])

    obs_text = "\n".join(f"- {o['content'][:300]}" for o in observations)

    response = await _anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=(
            f"Based on observations about {meta.get('description', 'a product')}, generate structured feature requests.\n\n"
            "Format each as:\n"
            "TITLE: <concise title>\n"
            "PROBLEM: <what problem users face>\n"
            "EVIDENCE: <what data supports this>\n"
            "SOLUTION: <proposed solution>\n"
            "PRIORITY: low/medium/high\n"
            "---"
        ),
        messages=[{"role": "user", "content": f"Observations:\n{obs_text}"}],
    )

    requests: list[dict] = []
    current: dict = {}
    for line in response.content[0].text.split("\n"):
        line = line.strip()
        if line.startswith("TITLE:"):
            if current:
                requests.append(current)
            current = {"title": line.replace("TITLE:", "").strip()}
        elif line.startswith("PROBLEM:"):
            current["problem"] = line.replace("PROBLEM:", "").strip()
        elif line.startswith("EVIDENCE:"):
            current["evidence"] = line.replace("EVIDENCE:", "").strip()
        elif line.startswith("SOLUTION:"):
            current["solution"] = line.replace("SOLUTION:", "").strip()
        elif line.startswith("PRIORITY:"):
            current["priority"] = line.replace("PRIORITY:", "").strip().lower()
    if current:
        requests.append(current)

    return {**state, "feature_requests": requests}


async def submit_requests(state: FeedbackState) -> FeedbackState:
    """Save feature requests to the DB and R2."""
    requests = state.get("feature_requests", [])

    async with async_session_factory() as session:
        for req in requests:
            fr = FeatureRequest(
                contract_id=state["contract_id"],
                title=req.get("title", ""),
                problem=req.get("problem", ""),
                evidence=req.get("evidence", ""),
                proposed_solution=req.get("solution", ""),
                priority=req.get("priority", "medium"),
            )
            session.add(fr)
        await session.commit()

    logger.info("feature_requests_submitted", count=len(requests), contract_slug=state["contract_slug"])
    return {**state, "submitted_count": len(requests), "status": "complete"}
