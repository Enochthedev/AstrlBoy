"""
Node functions for the community engagement graph.

Finds threads, scores them, drafts replies, self-critiques,
routes for approval (Reddit/Discord → Telegram, X/LinkedIn → auto),
posts, and logs everything.
"""

from uuid import uuid4

from anthropic import AsyncAnthropic

from core.config import settings
from core.constants import InteractionStatus, Platform
from core.logging import get_logger
from db.base import async_session_factory
from db.models.interactions import Interaction
from graphs.engagement.state import EngagementState
from skills.registry import skill_registry
from storage.r2 import r2_client

logger = get_logger("graphs.engagement.nodes")

_anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)


async def find_threads(state: EngagementState) -> EngagementState:
    """Search for relevant threads to engage with on the target platform.

    Uses find_engagement_opportunities skill when available for better
    scoring and suggested angles. Falls back to raw search otherwise.
    """
    meta = state["contract_meta"]
    keywords = meta.get("stream_keywords", [])
    platform = state.get("platform", "x")

    candidates: list[dict] = []

    # Prefer the dedicated engagement opportunities skill
    if await skill_registry.is_available("find_engagement_opportunities"):
        try:
            opportunities_skill = await skill_registry.get("find_engagement_opportunities")
            results = await opportunities_skill.execute(
                topics=keywords[:5],
                platforms=[platform],
                min_engagement=5,
                max_age_hours=12,
            )
            for r in results:
                candidates.append({
                    "title": r.get("summary", ""),
                    "url": r.get("url", ""),
                    "context": r.get("why_relevant", ""),
                    "suggested_angle": r.get("suggested_angle", ""),
                    "score": r.get("score", 0),
                })
        except Exception as exc:
            logger.warning("engagement_opportunities_failed", error=str(exc))

    # Fallback to raw search if no results
    if not candidates and await skill_registry.is_available("search"):
        query = f"{meta.get('description', '')} {' '.join(keywords[:3])}"
        try:
            search = await skill_registry.get("search")
            results = await search.execute(query=query, max_results=10)
            for r in results:
                candidates.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "context": r.get("content", "")[:500],
                })
        except Exception as exc:
            logger.warning("thread_search_failed", error=str(exc))

    return {**state, "candidate_threads": candidates}


async def score_threads(state: EngagementState) -> EngagementState:
    """Score each thread for engagement potential (0-10)."""
    candidates = state.get("candidate_threads", [])
    if not candidates:
        return {**state, "scored_threads": []}

    meta = state["contract_meta"]
    thread_text = "\n".join(
        f"- {t['title']}: {t['context'][:200]}" for t in candidates
    )

    response = await _anthropic.messages.create(
        model="claude-haiku-4-5",
        max_tokens=500,
        system=(
            f"Score each thread 0-10 for engagement value for {meta.get('description', 'the client')}.\n"
            "Consider: relevance, audience size, opportunity to add value.\n"
            "Respond: SCORE|TITLE per line."
        ),
        messages=[{"role": "user", "content": thread_text}],
    )

    scored: list[dict] = []
    for line in response.content[0].text.strip().split("\n"):
        if "|" in line:
            parts = line.split("|", 1)
            try:
                score = float(parts[0].strip())
                title = parts[1].strip()
                matching = next((t for t in candidates if title in t.get("title", "")), None)
                if matching:
                    scored.append({**matching, "score": score})
            except ValueError:
                continue

    return {**state, "scored_threads": scored}


async def filter_threads(state: EngagementState) -> EngagementState:
    """Keep only threads scoring 7 or above, capped at 5 per run."""
    scored = state.get("scored_threads", [])
    filtered = [t for t in scored if t.get("score", 0) >= 7][:5]
    return {**state, "filtered_threads": filtered}


async def draft_replies(state: EngagementState) -> EngagementState:
    """Draft a reply for each filtered thread."""
    threads = state.get("filtered_threads", [])
    meta = state["contract_meta"]
    drafts: list[dict] = []

    for thread in threads:
        response = await _anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            system=(
                f"You are astrlboy engaging in a community thread for {meta.get('description', 'a client')}.\n"
                f"Tone: {meta.get('tone', 'sharp, opinionated, concise')}\n"
                "Write a reply that adds genuine value. No self-promotion spam.\n"
                "Be specific, opinionated, and helpful."
            ),
            messages=[{"role": "user", "content": f"Thread: {thread['title']}\nContext: {thread['context']}"}],
        )
        drafts.append({
            **thread,
            "draft": response.content[0].text,
        })

    return {**state, "drafts": drafts}


async def route_approval(state: EngagementState) -> EngagementState:
    """Route drafts: X/LinkedIn auto-approve, Reddit/Discord → Telegram approval."""
    drafts = state.get("drafts", [])
    platform = state.get("platform", "x")
    approved: list[dict] = []

    # X and LinkedIn are auto-approved
    needs_approval = platform in (Platform.REDDIT, Platform.DISCORD)

    for draft_data in drafts:
        interaction_id = uuid4()

        async with async_session_factory() as session:
            interaction = Interaction(
                id=interaction_id,
                contract_id=state["contract_id"],
                platform=platform,
                thread_url=draft_data.get("url", ""),
                thread_context=draft_data.get("context", ""),
                draft=draft_data["draft"],
                status=InteractionStatus.PENDING if needs_approval else InteractionStatus.APPROVED,
            )
            session.add(interaction)
            await session.commit()

        if needs_approval:
            # Send to Telegram for approval
            if await skill_registry.is_available("draft_approval"):
                approval_skill = await skill_registry.get("draft_approval")
                try:
                    await approval_skill.execute(
                        interaction_id=str(interaction_id),
                        platform=platform,
                        draft=draft_data["draft"],
                        thread_context=draft_data.get("context", ""),
                    )
                except Exception as exc:
                    logger.warning("approval_send_failed", error=str(exc))
        else:
            approved.append({**draft_data, "interaction_id": interaction_id})

    return {**state, "approved_drafts": approved}


async def post(state: EngagementState) -> EngagementState:
    """Post approved drafts via the appropriate platform skill."""
    approved = state.get("approved_drafts", [])
    platform = state.get("platform", "x")
    posted = 0

    skill_name = f"post_{platform}"
    if await skill_registry.is_available(skill_name):
        skill = await skill_registry.get(skill_name)
        for draft_data in approved:
            try:
                await skill.execute(text=draft_data["draft"])
                posted += 1
            except Exception as exc:
                logger.warning("post_failed", platform=platform, error=str(exc))

    return {**state, "posted_count": posted}


async def log_interactions(state: EngagementState) -> EngagementState:
    """Log all interactions to R2 for training data."""
    drafts = state.get("drafts", [])
    for draft_data in drafts:
        try:
            await r2_client.dump(
                contract_slug=state["contract_slug"],
                entity_type="interactions",
                entity_id=uuid4(),
                data={
                    "platform": state.get("platform", "x"),
                    "thread_url": draft_data.get("url", ""),
                    "draft": draft_data.get("draft", ""),
                    "context": draft_data.get("context", ""),
                },
            )
        except Exception:
            pass

    return {**state, "status": "complete"}
