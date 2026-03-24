"""
Node functions for the content generation graph.

Each function is a node in the LangGraph graph. They execute sequentially
with conditional edges for the critique loop.
"""

import time
import uuid
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from anthropic import AsyncAnthropic
from sqlalchemy import select

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

# Words too common to count as meaningful topic overlap
_STOP_WORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "for", "in", "on", "to", "of", "and", "or", "with", "at", "by", "from",
    "as", "it", "its", "this", "that", "these", "those", "not", "but", "how",
    "why", "what", "when", "who", "will", "can", "has", "have", "had",
    "just", "now", "also", "more", "new", "about", "i", "you", "we", "they",
}


async def _is_recent_duplicate(
    title: str,
    draft: str,
    contract_id: uuid.UUID,
    days: int = 14,
) -> bool:
    """Check if similar content was published in the last N days.

    Compares the new title and first tweet/line against recent published content
    using significant-word overlap. 3+ shared non-stop words = too similar.

    This prevents the scheduler from posting about the same topic twice in two weeks,
    even when research surfaces the same trending story repeatedly.
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        async with async_session_factory() as session:
            result = await session.execute(
                select(Content.title, Content.body)
                .where(Content.contract_id == contract_id)
                .where(Content.status.in_(["published", "approved"]))
                .where(Content.created_at >= cutoff)
            )
            recent = result.all()

        if not recent:
            return False

        # Extract meaningful words from new content
        first_line = draft.split("\n")[0][:200].lower()
        new_words = set(title.lower().split() + first_line.split()) - _STOP_WORDS

        for old_title, old_body in recent:
            old_first_line = (old_body or "").split("\n")[0][:200].lower()
            old_words = set((old_title or "").lower().split() + old_first_line.split()) - _STOP_WORDS
            overlap = new_words & old_words
            if len(overlap) >= 3:
                logger.info(
                    "content_duplicate_skipped",
                    overlap=list(overlap)[:5],
                    new_title=title[:60],
                    old_title=(old_title or "")[:60],
                )
                return True

        return False
    except Exception:
        return False  # If check fails, don't block publishing


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

    # Build memory context string for the prompt
    memory_context = ""
    if state.get("context") or state.get("long_term_memories"):
        try:
            from memory.context_builder import format_context_for_prompt
            ctx = dict(state.get("context", {}))
            if state.get("long_term_memories"):
                ctx["long_term_memories"] = state["long_term_memories"]
            memory_context = format_context_for_prompt(ctx)
            if memory_context:
                memory_context = f"\n\n--- MEMORY CONTEXT ---\n{memory_context}\n--- END MEMORY ---\n"
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
        "- NO em dashes (—) as connectors. 'X — it does Y' is an AI tell. Use a period.\n"
        "- NO 'isn't just X — it's Y' or 'not just X, but also Y' patterns.\n"
        "- NO meta-commentary about sources: don't say '[Publication] buried the lead'. Just state the finding.\n"
        "- NO rhetorical question openers ('What if I told you...', 'Ever wonder why...')\n"
        "- NO parallel sentence triplets ('They do A. They also do B. They even do C.')\n"
        "- Lead with the sharpest, most specific fact. Not context-setting. Not a hook. The fact.\n"
        "- Mix sentence lengths naturally. Short ones land. Then a longer one builds the picture.\n"
        "- Use contractions — it's, they're, you've. Real humans do. AIs often don't.\n"
        "- Numbers beat vague time references. '18 months' not 'recently'. '$4.5B' not 'billions'.\n"
        f"{recent_tweets_context}\n"
        f"{memory_context}\n"
    )

    user_prompt = (
        f"Based on this research, decide whether this content should be a THREAD or a SINGLE TWEET.\n\n"
        f"Research:\n{state.get('research', 'No research available.')}\n\n"
        "DECISION RULES:\n"
        "- THREAD (3-7 tweets) if: the topic has multiple distinct angles, there's a story arc,\n"
        "  there's data + context + implication, or it would feel rushed in one tweet.\n"
        "- SINGLE TWEET if: it's a hot take, a one-liner observation, or the whole point lands in ≤280 chars.\n"
        "  Most content that can be said cleanly in one tweet SHOULD be one tweet.\n\n"
        "OUTPUT FORMAT:\n"
        "TITLE: <title for the piece>\n"
        "FORMAT: thread OR tweet\n"
        "BODY:\n"
        "<if thread: write each tweet on its own block like this>\n"
        "Tweet 1:\n"
        "<tweet text, max 280 chars>\n\n"
        "Tweet 2:\n"
        "<tweet text, max 280 chars>\n\n"
        "...\n\n"
        "<if single tweet: write just the tweet text, max 280 chars>\n"
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

    # Parse title, format flag, and body from response
    if "TITLE:" in text and "BODY:" in text:
        parts = text.split("BODY:", 1)
        header = parts[0]
        body = parts[1].strip()
        # Extract title (strip out the FORMAT line if present)
        title_block = header.replace("FORMAT: thread", "").replace("FORMAT: tweet", "").strip()
        title = title_block.replace("TITLE:", "").strip()

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
        "Check for these specific AI tells — any of these is an instant fail:\n"
        "1. Em dashes (—) used as connectors between clauses\n"
        "2. 'isn't just X — it's Y' or 'not just X, but also Y' constructions\n"
        "3. Meta-commentary about a source ('Forbes buried the lead', 'The report reveals')\n"
        "4. Rhetorical question openers ('What if I told you...', 'Have you noticed...')\n"
        "5. Parallel triplets ('They do A. They also do B. They even do C.')\n"
        "6. Vague time words when numbers are available ('recently', 'rapidly', 'significantly')\n"
        "7. Filler openers ('In today's world', 'It's worth noting', 'Let me be clear')\n\n"
        "Also check:\n"
        "8. Is every sentence earning its place? (cut filler)\n"
        "9. Is it opinionated and specific, not vague platitudes?\n"
        "10. Would the target audience actually stop scrolling for this?\n"
        f"11. Does it match this tone: {meta.get('tone', 'sharp, concise')}?\n\n"
        "Respond in this format:\n"
        "APPROVED: yes/no\n"
        "NOTES:\n<your critique notes>"
    )

    response = await _anthropic.messages.create(
        model="claude-haiku-4-5",
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

    Dedup-checks against the last 14 days before posting — if the same topic
    was already covered recently, skips silently rather than spamming the feed.
    In manual mode, sends drafts to Telegram for approval instead of posting directly.
    """
    meta = state["contract_meta"]
    platforms = meta.get("platforms", [])

    # Dedup check — don't post about the same topic twice in 14 days
    if await _is_recent_duplicate(
        title=state.get("title", ""),
        draft=state.get("draft", ""),
        contract_id=state["contract_id"],
    ):
        return {**state, "status": "skipped_duplicate"}

    if settings.agent_auto:
        # Auto mode — post immediately
        tweet_id = None
        for platform in platforms:
            skill_name = f"post_{platform}"
            if await skill_registry.is_available(skill_name):
                try:
                    skill = await skill_registry.get(skill_name)
                    draft = state["draft"]
                    if platform == "x":
                        # Detect thread format and use thread_x instead
                        from approval.telegram import _parse_thread_draft
                        tweets = _parse_thread_draft(draft)
                        if len(tweets) >= 2 and await skill_registry.is_available("thread_x"):
                            thread_skill = await skill_registry.get("thread_x")
                            result = await thread_skill.execute(tweets=tweets)
                            logger.info("content_published_thread", platform=platform, tweets=len(tweets), contract_slug=state["contract_slug"])
                        else:
                            result = await skill.execute(text=draft[:280])
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
        # Manual mode — send to Telegram for approval.
        # Send the FULL draft — cmd_approve handles thread detection and truncation.
        # Never truncate here or thread format gets destroyed before it can be parsed.
        if await skill_registry.is_available("draft_approval"):
            try:
                approval_skill = await skill_registry.get("draft_approval")
                await approval_skill.execute(
                    draft=state["draft"],
                    platform=platforms[0] if platforms else "x",
                    contract_slug=state["contract_slug"],
                    title=state.get("title", ""),
                )
                logger.info("draft_sent_for_approval", contract_slug=state["contract_slug"])
            except Exception as exc:
                logger.error("draft_approval_failed", error=str(exc))
        return {**state, "status": "pending_approval"}
