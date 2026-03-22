"""
Node functions for the content generation graph.

Each function is a node in the LangGraph graph. They execute sequentially
with conditional edges for the critique loop.
"""

import time
from uuid import uuid4

from anthropic import AsyncAnthropic

from core.config import settings
from core.logging import get_logger
from agent.service import agent_service
from db.base import async_session_factory
from db.models.content import Content
from graphs.content.state import ContentState
from skills.registry import skill_registry
from storage.r2 import r2_client

logger = get_logger("graphs.content.nodes")

_anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)


async def research_trends(state: ContentState) -> ContentState:
    """Pull relevant trend signals and do live research for content context.

    Uses analyze_trending_content and research_topic skills when available
    for richer context. Falls back to raw search otherwise.
    """
    start = time.monotonic()
    contract_meta = state["contract_meta"]
    keywords = contract_meta.get("stream_keywords", [])

    research = ""
    trend_signals: list[dict] = []

    # Try analyze_trending_content first — gives patterns + recommended angles
    try:
        if await skill_registry.is_available("analyze_trending_content") and keywords:
            analyze_skill = await skill_registry.get("analyze_trending_content")
            result = await analyze_skill.execute(topics=keywords[:5], timeframe_hours=48)
            if isinstance(result, dict):
                for thread in result.get("top_threads", []):
                    research += f"**{thread.get('title', thread.get('summary', ''))}**\n"
                    if thread.get("why_it_worked"):
                        research += f"Why it worked: {thread['why_it_worked']}\n"
                    research += "\n"
                    trend_signals.append(thread)
                if result.get("content_patterns"):
                    research += "Content patterns working now:\n"
                    research += "\n".join(f"- {p}" for p in result["content_patterns"]) + "\n\n"
                if result.get("recommended_angles"):
                    research += "Recommended angles:\n"
                    research += "\n".join(f"- {a}" for a in result["recommended_angles"]) + "\n\n"
    except Exception as exc:
        logger.warning("trending_analysis_failed", error=str(exc))

    # Supplement with research_topic for deeper context
    try:
        if not research and await skill_registry.is_available("research_topic"):
            topic = f"{contract_meta.get('description', '')} {' '.join(keywords[:3])}"
            research_skill = await skill_registry.get("research_topic")
            result = await research_skill.execute(topic=topic, depth="surface")
            if isinstance(result, dict):
                research = result.get("summary", "")
                if result.get("content_angles"):
                    research += "\n\nContent angles:\n"
                    research += "\n".join(f"- {a}" for a in result["content_angles"])
    except Exception as exc:
        logger.warning("research_topic_failed", error=str(exc))

    # Final fallback to raw search
    if not research:
        try:
            if await skill_registry.is_available("search"):
                query = f"{contract_meta.get('description', '')} {' '.join(keywords[:3])}"
                search_skill = await skill_registry.get("search")
                results = await search_skill.execute(query=query, max_results=5)
                for r in results:
                    research += f"**{r.get('title', '')}**\n{r.get('content', '')}\n\n"
                    trend_signals.append({
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": r.get("content", "")[:200],
                    })
        except Exception as exc:
            logger.warning("research_failed", error=str(exc))
            research = "No research available — proceeding with existing knowledge."

    duration = int((time.monotonic() - start) * 1000)
    await agent_service.log_action(
        entity_type="content",
        entity_id=state.get("content_id", uuid4()),
        action="research_trends",
        outcome=f"found {len(trend_signals)} signals",
        contract_slug=state["contract_slug"],
        duration_ms=duration,
    )

    return {**state, "research": research, "trend_signals": trend_signals}


async def generate_draft(state: ContentState) -> ContentState:
    """Generate a content draft using Claude based on research and client tone."""
    start = time.monotonic()
    meta = state["contract_meta"]

    # Fetch recent tweets to avoid repeats
    recent_tweets_context = ""
    try:
        if await skill_registry.is_available("get_timeline"):
            timeline_skill = await skill_registry.get("get_timeline")
            recent = await timeline_skill.execute(max_results=10)
            if recent:
                recent_tweets_context = (
                    "\n\nYour recent tweets (DO NOT repeat these topics or angles):\n"
                    + "\n".join(f"- {t['text'][:100]}" for t in recent)
                )
    except Exception:
        pass

    system_prompt = (
        f"You are astrlboy, an autonomous AI agent writing content for {meta.get('description', 'a client')}.\n\n"
        f"Tone: {meta.get('tone', 'sharp, opinionated, concise')}\n"
        f"Content type: {state['content_type']}\n\n"
        "Rules:\n"
        "- Write like a human expert, not an AI\n"
        "- Be opinionated and specific\n"
        "- Cut filler — every sentence should earn its place\n"
        "- No 'in today's world', 'it's important to note', or other AI slop\n"
        "- No hashtags unless the topic demands it\n"
        f"{recent_tweets_context}\n"
    )

    user_prompt = (
        f"Write a {state['content_type']} based on this research:\n\n"
        f"{state.get('research', 'No research available.')}\n\n"
        "Return your response in this format:\n"
        "TITLE: <title>\n"
        "BODY:\n<full content>"
    )

    response = await _anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = response.content[0].text
    title = ""
    body = text

    # Parse title and body from response
    if "TITLE:" in text and "BODY:" in text:
        parts = text.split("BODY:", 1)
        title = parts[0].replace("TITLE:", "").strip()
        body = parts[1].strip()

    duration = int((time.monotonic() - start) * 1000)
    await agent_service.log_action(
        entity_type="content",
        entity_id=state.get("content_id", uuid4()),
        action="generate_draft",
        outcome="draft_generated",
        contract_slug=state["contract_slug"],
        duration_ms=duration,
    )

    return {**state, "title": title, "draft": body, "revision_count": 0}


async def self_critique(state: ContentState) -> ContentState:
    """Critique the draft as a sharp human editor would.

    Returns approval status and critique notes. If not approved,
    the graph loops back to revise.
    """
    start = time.monotonic()
    meta = state["contract_meta"]

    system_prompt = (
        "You are a ruthless content editor. Your job is to critique this draft.\n\n"
        "Check for:\n"
        "1. Does it sound like AI wrote it? (instant fail)\n"
        "2. Is every sentence earning its place? (cut filler)\n"
        "3. Is it opinionated and specific? (not vague platitudes)\n"
        "4. Would the target audience actually engage with this?\n"
        f"5. Does it match this tone: {meta.get('tone', 'sharp, concise')}?\n\n"
        "Respond in this format:\n"
        "APPROVED: yes/no\n"
        "NOTES:\n<your critique notes>"
    )

    response = await _anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=system_prompt,
        messages=[{"role": "user", "content": state["draft"]}],
    )

    text = response.content[0].text
    is_approved = "APPROVED: yes" in text.lower() or "approved: yes" in text.lower()
    notes = text.split("NOTES:", 1)[1].strip() if "NOTES:" in text else text

    duration = int((time.monotonic() - start) * 1000)
    await agent_service.log_action(
        entity_type="content",
        entity_id=state.get("content_id", uuid4()),
        action="self_critique",
        outcome="approved" if is_approved else "needs_revision",
        contract_slug=state["contract_slug"],
        duration_ms=duration,
    )

    return {**state, "critique_notes": notes, "is_approved": is_approved}


async def revise(state: ContentState) -> ContentState:
    """Revise the draft based on critique notes."""
    start = time.monotonic()
    revision_count = state.get("revision_count", 0) + 1

    system_prompt = (
        "You are revising a content draft based on editorial feedback.\n"
        "Apply every note. Keep the same structure but improve quality.\n"
        "Do not acknowledge the feedback — just output the revised content."
    )

    user_prompt = (
        f"Original draft:\n{state['draft']}\n\n"
        f"Editor notes:\n{state['critique_notes']}\n\n"
        "Output the revised version:"
    )

    response = await _anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    duration = int((time.monotonic() - start) * 1000)
    await agent_service.log_action(
        entity_type="content",
        entity_id=state.get("content_id", uuid4()),
        action="revise",
        outcome=f"revision_{revision_count}",
        contract_slug=state["contract_slug"],
        duration_ms=duration,
    )

    return {**state, "draft": response.content[0].text, "revision_count": revision_count}


async def approve_or_escalate(state: ContentState) -> ContentState:
    """Escalate to Wave when the critique loop has failed twice."""
    await agent_service.escalate(
        reason=f"Content self-critique failed {state['revision_count']} times",
        context={
            "contract_slug": state["contract_slug"],
            "content_type": state["content_type"],
            "title": state.get("title", ""),
            "critique_notes": state.get("critique_notes", ""),
        },
    )
    return {**state, "status": "escalated"}


async def save(state: ContentState) -> ContentState:
    """Persist the approved content to the DB and dump raw I/O to R2."""
    content_id = uuid4()

    async with async_session_factory() as session:
        content = Content(
            id=content_id,
            contract_id=state["contract_id"],
            type=state["content_type"],
            title=state.get("title", ""),
            body=state["draft"],
            critique_notes=state.get("critique_notes", ""),
            revision_count=state.get("revision_count", 0),
            status="approved",
        )
        session.add(content)
        await session.commit()

    # Dump to R2 for training data
    try:
        r2_key = await r2_client.dump(
            contract_slug=state["contract_slug"],
            entity_type="content",
            entity_id=content_id,
            data={
                "title": state.get("title", ""),
                "body": state["draft"],
                "research": state.get("research", ""),
                "critique_notes": state.get("critique_notes", ""),
                "revision_count": state.get("revision_count", 0),
                "model": "claude-sonnet-4-6",
            },
        )
    except Exception:
        r2_key = ""

    return {**state, "content_id": content_id, "r2_key": r2_key, "status": "approved"}


async def publish(state: ContentState) -> ContentState:
    """Post the content via the appropriate platform skill.

    In manual mode, sends drafts to Telegram for approval instead of posting directly.
    """
    meta = state["contract_meta"]
    platforms = meta.get("platforms", [])

    if settings.agent_auto:
        # Auto mode — post immediately
        tweet_id = None
        for platform in platforms:
            skill_name = f"post_{platform}"
            if await skill_registry.is_available(skill_name):
                try:
                    skill = await skill_registry.get(skill_name)
                    result = await skill.execute(text=state["draft"][:280] if platform == "x" else state["draft"])
                    # Store tweet_id for performance tracking
                    if platform == "x" and isinstance(result, dict) and result.get("tweet_id"):
                        tweet_id = result["tweet_id"]
                        # Update the content record with the tweet_id
                        try:
                            from sqlalchemy import update
                            async with async_session_factory() as session:
                                await session.execute(
                                    update(Content)
                                    .where(Content.id == state["content_id"])
                                    .values(tweet_id=tweet_id, status="published", platform="x")
                                )
                                await session.commit()
                        except Exception:
                            pass
                    logger.info("content_published", platform=platform, contract_slug=state["contract_slug"])
                except Exception as exc:
                    logger.error("publish_failed", platform=platform, error=str(exc))
        return {**state, "status": "published", "tweet_id": tweet_id}
    else:
        # Manual mode — send to Telegram for approval
        if await skill_registry.is_available("draft_approval"):
            try:
                approval_skill = await skill_registry.get("draft_approval")
                await approval_skill.execute(
                    draft=state["draft"][:280] if "x" in platforms else state["draft"],
                    platform=platforms[0] if platforms else "x",
                    contract_slug=state["contract_slug"],
                    title=state.get("title", ""),
                )
                logger.info("draft_sent_for_approval", contract_slug=state["contract_slug"])
            except Exception as exc:
                logger.error("draft_approval_failed", error=str(exc))
        return {**state, "status": "pending_approval"}
