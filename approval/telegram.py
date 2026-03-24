"""
Telegram bot for the operator approval queue and monitoring.

Wave can approve/reject drafts, pause/resume the agent, check status,
and monitor contracts, content, trends, and escalations.
"""

import json
import re
import time
from collections import deque
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from anthropic import AsyncAnthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from core.config import settings
from core.constants import (
    ApplicationStatus,
    ContentStatus,
    ContractStatus,
    ExperimentStatus,
    InteractionStatus,
)
from core.logging import get_logger
from db.base import async_session_factory
from db.models.content import Content
from db.models.contracts import Contract
from db.models.escalations import Escalation
from db.models.experiments import Experiment
from db.models.interactions import Interaction
from db.models.job_applications import JobApplication
from db.models.trend_signals import TrendSignal

logger = get_logger("approval.telegram")


# ── Thread parsing helpers ────────────────────────────────────────


def _parse_thread_draft(draft: str) -> list[str]:
    """Parse a thread-style draft into individual tweets.

    Supports formats:
    - "Tweet 1:\\n...\\n\\nTweet 2:\\n..." (numbered tweet labels)
    - Separated by "---" delimiters

    Returns a list of tweet texts. If the draft is not a thread
    (single tweet or unrecognized format), returns an empty list.
    """
    # Try "Tweet N:" format — the autonomous agent uses this
    parts = re.split(r"Tweet\s+\d+:\s*\n", draft)
    tweets = [p.strip() for p in parts if p.strip()]
    if len(tweets) >= 2:
        return tweets

    # Try "---" separator — used by apply_to_url deliverables
    parts = [p.strip() for p in draft.split("---") if p.strip()]
    if len(parts) >= 2:
        return parts

    return []


def _extract_post_actions(thread_context: str) -> tuple[str, list[dict]]:
    """Extract post-approval actions from thread_context.

    Actions are stored as a JSON array after a ---POST_ACTIONS--- delimiter.

    Returns:
        Tuple of (clean_context, actions_list).
    """
    delimiter = "---POST_ACTIONS---"
    if delimiter not in thread_context:
        return thread_context, []

    try:
        clean_context, actions_json = thread_context.split(delimiter, 1)
        actions = json.loads(actions_json.strip())
        if isinstance(actions, list):
            return clean_context.strip(), actions
    except (ValueError, json.JSONDecodeError):
        pass

    return thread_context, []


# ── Approval commands ──────────────────────────────────────────────


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve a pending interaction and post it immediately.

    Detects thread-style drafts (multiple tweets) and uses thread_x.
    Executes any post-approval actions stored in thread_context
    (e.g. sending a follow-up email after posting).

    Usage: /approve <interaction_id>
    """
    if not context.args:
        await update.message.reply_text("Usage: /approve <interaction_id>")
        return

    interaction_id = context.args[0]
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Interaction).where(Interaction.id == UUID(interaction_id))
            )
            interaction = result.scalar_one_or_none()
            if not interaction:
                await update.message.reply_text(f"Interaction {interaction_id} not found.")
                return

            interaction.status = InteractionStatus.APPROVED
            interaction.posted_at = datetime.now(timezone.utc)
            draft_text = interaction.draft
            platform = interaction.platform
            thread_context = interaction.thread_context or ""
            await session.commit()

        # Extract any post-approval actions before posting
        _, post_actions = _extract_post_actions(thread_context)

        # Post the approved content
        posted = False
        post_result: dict = {}
        if draft_text and platform:
            try:
                from skills.registry import skill_registry

                if platform == "x":
                    # Detect thread-style drafts and use thread_x
                    tweets = _parse_thread_draft(draft_text)
                    if len(tweets) >= 2 and await skill_registry.is_available("thread_x"):
                        thread_skill = await skill_registry.get("thread_x")
                        post_result = await thread_skill.execute(tweets=tweets)
                        posted = True
                        thread_url = post_result.get("thread_url", "")
                        count = post_result.get("count", len(tweets))
                        await update.message.reply_text(
                            f"Approved & posted thread ({count} tweets)!\n\n"
                            f"{tweets[0][:200]}\n\n"
                            f"Thread: {thread_url}"
                        )
                    else:
                        # Single tweet
                        if await skill_registry.is_available("post_x"):
                            skill = await skill_registry.get("post_x")
                            post_result = await skill.execute(text=draft_text[:280])
                            posted = True
                            tweet_id = post_result.get("tweet_id", "")
                            await update.message.reply_text(
                                f"Approved & posted!\n\n{draft_text[:200]}\n\nTweet ID: {tweet_id}"
                            )
                else:
                    skill_name = f"post_{platform}"
                    if await skill_registry.is_available(skill_name):
                        skill = await skill_registry.get(skill_name)
                        post_result = await skill.execute(text=draft_text)
                        posted = True
                        await update.message.reply_text(f"Approved & posted on {platform}!")

            except Exception as exc:
                logger.error("post_after_approve_failed", error=str(exc))
                await update.message.reply_text(f"Approved but posting failed: {exc}")

        # Execute post-approval actions (e.g. send follow-up email)
        if posted and post_actions:
            await _execute_post_actions(post_actions, post_result, update)

        if not posted:
            await update.message.reply_text(f"Approved: {interaction_id}")
        logger.info("interaction_approved", interaction_id=interaction_id)
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def _execute_post_actions(
    actions: list[dict],
    post_result: dict,
    update: Update,
) -> None:
    """Execute follow-up actions after a draft is approved and posted.

    Supports action types:
    - send_email: Send an email via the send_email skill. Body can contain
      {thread_url} and {tweet_id} placeholders replaced with actual values.

    Args:
        actions: List of action dicts from thread_context.
        post_result: Result dict from posting (contains thread_url, tweet_id, etc.).
        update: Telegram update for sending status messages.
    """
    from skills.registry import skill_registry

    thread_url = post_result.get("thread_url", "")
    tweet_id = post_result.get("tweet_id", "")
    # For threads, grab the first tweet ID
    tweet_ids = post_result.get("thread_tweet_ids", [])
    if not tweet_id and tweet_ids:
        tweet_id = tweet_ids[0]

    for action in actions:
        action_type = action.get("type")
        try:
            if action_type == "send_email" and await skill_registry.is_available("send_email"):
                send_skill = await skill_registry.get("send_email")

                body = action.get("body", "")
                subject = action.get("subject", "")
                # Replace placeholders with actual posted URLs
                body = body.replace("{thread_url}", thread_url)
                body = body.replace("{tweet_id}", tweet_id)
                subject = subject.replace("{thread_url}", thread_url)

                await send_skill.execute(
                    to=action["to"],
                    subject=subject,
                    body=body,
                )
                await update.message.reply_text(f"Follow-up email sent to {action['to']}")
                logger.info("post_action_email_sent", to=action["to"])
            else:
                logger.warning("post_action_unknown", action_type=action_type)
        except Exception as exc:
            logger.warning("post_action_failed", action_type=action_type, error=str(exc))
            await update.message.reply_text(f"Follow-up action failed ({action_type}): {exc}")


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reject a pending interaction.

    Usage: /reject <interaction_id>
    """
    if not context.args:
        await update.message.reply_text("Usage: /reject <interaction_id>")
        return

    interaction_id = context.args[0]
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Interaction).where(Interaction.id == UUID(interaction_id))
            )
            interaction = result.scalar_one_or_none()
            if not interaction:
                await update.message.reply_text(f"Interaction {interaction_id} not found.")
                return

            interaction.status = InteractionStatus.REJECTED
            await session.commit()

        await update.message.reply_text(f"Rejected: {interaction_id}")
        logger.info("interaction_rejected", interaction_id=interaction_id)
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


# ── Agent control ──────────────────────────────────────────────────


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause all agent activity.

    Usage: /pause
    """
    settings.agent_paused = True
    await update.message.reply_text("Agent paused. Use /resume to restart.")
    logger.info("agent_paused_via_telegram")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resume agent activity.

    Usage: /resume
    """
    settings.agent_paused = False
    await update.message.reply_text("Agent resumed.")
    logger.info("agent_resumed_via_telegram")


async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch to auto mode — posts go live without approval.

    Usage: /auto
    """
    settings.agent_auto = True
    await update.message.reply_text(
        "AUTO MODE ON\n\n"
        "astrlboy will post, reply, and engage without asking.\n"
        "Escalations and errors still come to you.\n"
        "Use /manual to require approval first."
    )
    logger.info("auto_mode_enabled")


async def cmd_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch to manual mode — all posts sent for approval first.

    Usage: /manual
    """
    settings.agent_auto = False
    await update.message.reply_text(
        "MANUAL MODE ON\n\n"
        "All posts will be sent here for approval before going live.\n"
        "Use /auto to let astrlboy run free."
    )
    logger.info("manual_mode_enabled")


# ── Monitoring commands ────────────────────────────────────────────


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show agent status: paused state, pending approvals, active contracts, unresolved escalations.

    Usage: /status
    """
    try:
        async with async_session_factory() as session:
            pending = (await session.execute(
                select(func.count()).select_from(Interaction).where(
                    Interaction.status == InteractionStatus.PENDING
                )
            )).scalar() or 0

            active_contracts = (await session.execute(
                select(func.count()).select_from(Contract).where(
                    Contract.status == ContractStatus.ACTIVE
                )
            )).scalar() or 0

            unresolved = (await session.execute(
                select(func.count()).select_from(Escalation).where(
                    Escalation.resolved.is_(False)
                )
            )).scalar() or 0

        mode = "AUTO" if settings.agent_auto else "MANUAL"
        status_text = (
            f"{'PAUSED' if settings.agent_paused else 'RUNNING'} | Mode: {mode}\n"
            f"Active contracts: {active_contracts}\n"
            f"Pending approvals: {pending}\n"
            f"Unresolved escalations: {unresolved}"
        )
    except Exception:
        mode = "AUTO" if settings.agent_auto else "MANUAL"
        status_text = (
            f"{'PAUSED' if settings.agent_paused else 'RUNNING'} | Mode: {mode}\n"
            "(DB unavailable — cannot fetch counts)"
        )

    await update.message.reply_text(status_text)


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all pending approvals.

    Usage: /pending
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(Interaction)
            .where(Interaction.status == InteractionStatus.PENDING)
            .order_by(Interaction.created_at.desc())
            .limit(10)
        )
        pending = result.scalars().all()

    if not pending:
        await update.message.reply_text("No pending approvals.")
        return

    lines = []
    for interaction in pending:
        preview = (interaction.draft or "")[:100]
        lines.append(
            f"[{interaction.platform}] {preview}...\n"
            f"/approve {interaction.id}\n/reject {interaction.id}"
        )
    await update.message.reply_text("\n---\n".join(lines))


async def cmd_contracts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all contracts and their status.

    Usage: /contracts
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(Contract).order_by(Contract.created_at.desc())
        )
        contracts = result.scalars().all()

    if not contracts:
        await update.message.reply_text("No contracts yet.")
        return

    lines = []
    for c in contracts:
        emoji = {"active": "+", "paused": "||", "completed": "ok"}.get(c.status, "?")
        lines.append(f"[{emoji}] {c.client_name} ({c.client_slug}) — {c.status}")
    await update.message.reply_text("\n".join(lines))


async def cmd_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the 5 most recent content pieces.

    Usage: /content
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(Content).order_by(Content.created_at.desc()).limit(5)
        )
        items = result.scalars().all()

    if not items:
        await update.message.reply_text("No content yet.")
        return

    lines = []
    for item in items:
        date = item.created_at.strftime("%b %d") if item.created_at else "?"
        lines.append(f"[{item.status}] {item.title or item.type} — {item.platform} ({date})")
    await update.message.reply_text("\n".join(lines))


async def cmd_trends(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the 5 most recent trend signals.

    Usage: /trends
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(TrendSignal).order_by(TrendSignal.captured_at.desc()).limit(5)
        )
        signals = result.scalars().all()

    if not signals:
        await update.message.reply_text("No trend signals yet.")
        return

    lines = []
    for s in signals:
        score_str = f" (score: {s.score:.1f})" if s.score is not None else ""
        preview = (s.signal or "")[:80]
        lines.append(f"[{s.source}]{score_str} {preview}")
    await update.message.reply_text("\n".join(lines))


async def cmd_escalations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show unresolved escalations.

    Usage: /escalations
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(Escalation)
            .where(Escalation.resolved.is_(False))
            .order_by(Escalation.created_at.desc())
            .limit(5)
        )
        items = result.scalars().all()

    if not items:
        await update.message.reply_text("No unresolved escalations.")
        return

    lines = []
    for e in items:
        date = e.created_at.strftime("%b %d %H:%M") if e.created_at else "?"
        lines.append(f"[{date}] {e.reason}\nID: {e.id}")
    await update.message.reply_text("\n---\n".join(lines))


async def cmd_experiments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show running experiments.

    Usage: /experiments
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(Experiment)
            .where(Experiment.status == ExperimentStatus.RUNNING)
            .order_by(Experiment.started_at.desc())
            .limit(5)
        )
        items = result.scalars().all()

    if not items:
        await update.message.reply_text("No running experiments.")
        return

    lines = []
    for exp in items:
        lines.append(f"{exp.title}\n{exp.hypothesis[:80]}")
    await update.message.reply_text("\n---\n".join(lines))


async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent job applications.

    Usage: /jobs
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(JobApplication).order_by(JobApplication.sent_at.desc()).limit(5)
        )
        items = result.scalars().all()

    if not items:
        await update.message.reply_text("No job applications yet.")
        return

    lines = []
    for j in items:
        date = j.sent_at.strftime("%b %d") if j.sent_at else "?"
        lines.append(f"[{j.status}] {j.role} @ {j.company} ({date})")
    await update.message.reply_text("\n".join(lines))


async def cmd_trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search for what's trending right now across active contracts.

    Usage: /trending [optional keywords]
    """
    await update.message.reply_text("Searching trends...")

    # Use custom keywords if provided, otherwise pull from active contracts
    custom_query = " ".join(context.args) if context.args else None

    try:
        from skills.registry import skill_registry

        if not await skill_registry.is_available("search"):
            await update.message.reply_text("Search skill not available.")
            return

        search = await skill_registry.get("search")

        if custom_query:
            query = custom_query
        else:
            # Pull keywords from active contracts
            async with async_session_factory() as session:
                result = await session.execute(
                    select(Contract).where(Contract.status == ContractStatus.ACTIVE)
                )
                contracts = result.scalars().all()

            if contracts:
                keywords = []
                for c in contracts:
                    keywords.extend((c.meta or {}).get("stream_keywords", []))
                query = " ".join(keywords[:5]) or "AI crypto web3 trending"
            else:
                query = "AI crypto web3 trending"

        results = await search.execute(query=query, max_results=5)
        if not results:
            await update.message.reply_text("Nothing found.")
            return

        lines = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "No title")[:80]
            url = r.get("url", "")
            # Strip markdown artifacts from Tavily content for clean display
            raw = r.get("content", "")
            snippet = raw.replace("#", "").replace("*", "").replace("🚀", "").strip()
            # Truncate to last complete sentence within 120 chars
            if len(snippet) > 120:
                cut = snippet[:120].rfind(".")
                snippet = snippet[: cut + 1] if cut > 40 else snippet[:120] + "…"
            lines.append(f"{i}. {title}\n{snippet}\n{url}")

        await update.message.reply_text("\n\n".join(lines))
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_makepost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger content generation for an active contract.

    Usage: /makepost [contract_slug]
    If no slug is provided and there's only one active contract, uses that.
    """
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Contract).where(Contract.status == ContractStatus.ACTIVE)
            )
            contracts = result.scalars().all()

        if not contracts:
            await update.message.reply_text("No active contracts. Create one first via the API.")
            return

        # If slug provided, find it; otherwise use the only active one
        if context.args:
            slug = context.args[0]
            contract = next((c for c in contracts if c.client_slug == slug), None)
            if not contract:
                slugs = ", ".join(c.client_slug for c in contracts)
                await update.message.reply_text(f"Contract '{slug}' not found. Active: {slugs}")
                return
        elif len(contracts) == 1:
            contract = contracts[0]
        else:
            slugs = ", ".join(c.client_slug for c in contracts)
            await update.message.reply_text(f"Multiple contracts. Specify one:\n/makepost <slug>\n\nActive: {slugs}")
            return

        await update.message.reply_text(f"Generating content for {contract.client_name}...")

        from graphs.content.graph import content_graph
        result = await content_graph.run(contract, content_type="post")

        status = result.get("status", "unknown")
        title = result.get("title", "")
        draft_preview = (result.get("draft", "")[:200] + "...") if result.get("draft") else ""

        await update.message.reply_text(
            f"Done — {status}\n"
            f"Title: {title}\n\n"
            f"{draft_preview}"
        )
    except Exception as exc:
        logger.error("makepost_failed", error=str(exc))
        await update.message.reply_text(f"Error: {exc}")


async def cmd_addcontract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a new client contract.

    Usage: /addcontract <slug> <name> <description>
    Example: /addcontract mentorable Mentorable Onchain mentorship marketplace on Base
    """
    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /addcontract <slug> <name> <description>\n\n"
            "Example:\n/addcontract mentorable Mentorable Onchain mentorship marketplace on Base"
        )
        return

    slug = context.args[0].lower()
    name = context.args[1]
    description = " ".join(context.args[2:])

    try:
        async with async_session_factory() as session:
            # Check if slug already exists
            result = await session.execute(
                select(Contract).where(Contract.client_slug == slug)
            )
            if result.scalar_one_or_none():
                await update.message.reply_text(f"Contract '{slug}' already exists.")
                return

            contract = Contract(
                client_name=name,
                client_slug=slug,
                status="active",
                client_db_url="",
                meta={
                    "description": description,
                    "website": "",
                    "tone": "sharp, opinionated, concise",
                    "content_types": ["post", "trend"],
                    "competitors": [],
                    "subreddits": [],
                    "discord_servers": [],
                    "stream_keywords": [],
                    "briefing_recipients": [],
                    "feature_request_endpoint": "",
                    "platforms": ["x"],
                    "active_skills": ["search", "serp", "post_x"],
                },
            )
            session.add(contract)
            await session.commit()

        await update.message.reply_text(f"Contract '{name}' ({slug}) created and active.")
        logger.info("contract_created_via_telegram", slug=slug)
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_mentions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check recent mentions, classify them, and reply appropriately.

    Uses the same classification as the scheduled mentions job:
    - relevant: engage genuinely
    - ask_ai: dismiss with personality (not Grok)
    - spam: skip
    - troll: ignore or sharp one-liner

    Usage: /mentions
    """
    await update.message.reply_text("Checking mentions...")

    try:
        import json as _json

        from skills.registry import skill_registry

        if not await skill_registry.is_available("get_mentions"):
            await update.message.reply_text("Mentions skill not available.")
            return

        mentions_skill = await skill_registry.get("get_mentions")
        mentions = await mentions_skill.execute(max_results=5)

        if not mentions:
            await update.message.reply_text("No recent mentions.")
            return

        # Show mentions with classification
        if not await skill_registry.is_available("post_x"):
            # Just show mentions without replying
            lines = [f"@{m['author_username']}: {m['text'][:120]}" for m in mentions]
            await update.message.reply_text("Recent mentions:\n\n" + "\n---\n".join(lines))
            return

        post_skill = await skill_registry.get("post_x")
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)

        # Import classification prompts from scheduler
        from scheduler.jobs import DISMISSAL_PROMPT, MENTION_CLASSIFY_PROMPT, TROLL_PROMPT

        replied = 0
        skipped = 0
        pending = 0
        for m in mentions[:5]:
            try:
                # Classify first
                classify_resp = await _client.messages.create(
                    model="claude-haiku-4-5",
                    max_tokens=100,
                    system=MENTION_CLASSIFY_PROMPT,
                    messages=[{"role": "user", "content": f"@{m['author_username']} said: {m['text']}"}],
                )
                try:
                    classification = _json.loads(classify_resp.content[0].text.strip())
                except _json.JSONDecodeError:
                    classification = {"category": "relevant"}

                category = classification.get("category", "relevant")

                if category == "spam":
                    skipped += 1
                    continue

                if category == "troll":
                    resp = await _client.messages.create(
                        model="claude-haiku-4-5",
                        max_tokens=200,
                        system=TROLL_PROMPT,
                        messages=[{"role": "user", "content": f"@{m['author_username']} said: {m['text']}"}],
                    )
                    reply_text = resp.content[0].text.strip()
                    if reply_text == "SKIP" or not reply_text:
                        skipped += 1
                        continue
                    reply_text = reply_text[:280]
                elif category == "ask_ai":
                    resp = await _client.messages.create(
                        model="claude-haiku-4-5",
                        max_tokens=200,
                        system=DISMISSAL_PROMPT,
                        messages=[{"role": "user", "content": f"@{m['author_username']} said: {m['text']}"}],
                    )
                    reply_text = resp.content[0].text.strip()[:280]
                else:
                    response = await _client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=280,
                        system=(
                            "You are astrlboy, an autonomous AI agent on X. You work as a freelance "
                            "contractor in AI, Web3, and tech.\n"
                            "Reply to this mention. Be sharp, concise, human. Max 280 chars."
                        ),
                        messages=[{"role": "user", "content": f"@{m['author_username']} said: {m['text']}"}],
                    )
                    reply_text = response.content[0].text[:280]

                tag = f"[{category}]"
                if settings.agent_auto:
                    await post_skill.execute(text=reply_text, reply_to_id=m["id"])
                    replied += 1
                    await update.message.reply_text(f"{tag} @{m['author_username']}: {reply_text[:150]}")
                else:
                    async with async_session_factory() as session:
                        interaction = Interaction(
                            platform="x",
                            draft=reply_text,
                            status=InteractionStatus.PENDING,
                            thread_context=f"Reply to @{m['author_username']}: {m['text'][:200]}",
                        )
                        session.add(interaction)
                        await session.commit()
                        pending += 1
                        await update.message.reply_text(
                            f"{tag} Reply to @{m['author_username']}:\n\n{reply_text}\n\n"
                            f"/approve {interaction.id}\n/reject {interaction.id}"
                        )
            except Exception as exc:
                logger.warning("mention_reply_failed", mention_id=m["id"], error=str(exc))

        total = len(mentions[:5])
        summary = f"Done — {total} mentions: {replied} replied, {pending} pending, {skipped} skipped"
        await update.message.reply_text(summary)
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


# ── Conversation history ──────────────────────────────────────────

# Per-chat rolling conversation history passed to the autonomous agent.
#
# Design: rolling window + summary compression, like how a human remembers a chat.
# - Keep the last 8 messages (4 turns) verbatim — the "working memory"
# - When we hit that limit, summarize the oldest half with Haiku and discard them
# - The summary is prepended as a synthetic turn so the agent still has context
# - A /ctx command pins a persistent context note for the whole session
# - A /newchat command resets the window entirely (new session, fresh slate)
# - Entries auto-expire after 2 hours of inactivity
#
# Token savings vs old approach (20 messages, no summarization):
# - System prompt: ~90% saved via cache_control in autonomous.py
# - History: capped at ~10 messages max (8 fresh + 2 for summary) vs unbounded
_HISTORY_MAX_FRESH = 8        # fresh verbatim messages to keep (4 turns)
_HISTORY_SUMMARIZE_AT = 6     # compress oldest turns when fresh window hits this
_HISTORY_TTL_SECONDS = 7200   # 2 hours

# Alert Wave when a single session exceeds this Claude spend.
# At $3/MTok input + $15/MTok output, 25 cents ≈ a heavy 5-turn session.
_SESSION_COST_ALERT_CENTS = 25

# {chat_id: {
#   "messages": deque[dict],        # recent fresh messages (user/assistant)
#   "last_ts": float,
#   "summary": str | None,          # compressed summary of older turns
#   "session_context": str | None,  # pinned context from /ctx command
#   "session_tokens_in": int,       # cumulative Claude input tokens this session
#   "session_tokens_out": int,      # cumulative Claude output tokens this session
#   "cost_alerted": bool,           # true once we've sent the cost warning
# }}
_chat_history: dict[str, dict] = {}


async def _summarize_old_turns(messages: list[dict]) -> str:
    """Compress old conversation turns into a brief summary using Haiku.

    Called when history exceeds the fresh window. Uses Haiku (fast + cheap) to
    distill what was discussed so the agent still has memory without the full
    token cost of keeping every message verbatim.

    Args:
        messages: List of {"role": str, "content": str} dicts to summarize.

    Returns:
        A compact summary string stored as session memory.
    """
    formatted = "\n".join(
        f"{m['role'].upper()}: {str(m['content'])[:300]}"
        for m in messages
    )
    try:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            system=(
                "You summarize conversations between Wave (human operator) and astrlboy (AI agent). "
                "Write 2-3 sentences covering: what was requested, what tools/actions were used, "
                "and any key outcomes or decisions. Be specific. Plain text only."
            ),
            messages=[{"role": "user", "content": f"Summarize:\n\n{formatted}"}],
        )
        return resp.content[0].text.strip()
    except Exception:
        turn_count = len(messages) // 2
        return f"{turn_count} earlier turns (summary unavailable)"


async def _maybe_compress_history(chat_id: str) -> None:
    """Compress oldest turns into a summary when the fresh window is full.

    Triggered before each new user message. If we've accumulated enough messages,
    the oldest half gets summarized and discarded — keeping the deque lean.

    Args:
        chat_id: The Telegram chat ID to compress.
    """
    entry = _chat_history.get(chat_id)
    if not entry:
        return

    messages = list(entry["messages"])
    if len(messages) < _HISTORY_SUMMARIZE_AT:
        return

    # Split: compress oldest half, keep newest half verbatim
    split = len(messages) // 2
    to_compress = messages[:split]
    to_keep = messages[split:]

    # If there's already a summary, include it so we roll it forward
    existing = entry.get("summary")
    if existing:
        summary_input = [
            {"role": "user", "content": f"[Previous summary: {existing}]"},
            {"role": "assistant", "content": "Got it."},
            *to_compress,
        ]
    else:
        summary_input = to_compress

    new_summary = await _summarize_old_turns(summary_input)

    entry["summary"] = new_summary
    entry["messages"] = deque(to_keep, maxlen=_HISTORY_MAX_FRESH)

    # Persist the summary to long-term memory so the agent can recall this
    # conversation in future sessions — even after /newchat clears the window.
    try:
        from memory.mem0_client import agent_memory
        if agent_memory.available:
            await agent_memory.add(
                f"Telegram session memory: {new_summary}",
                category="session_memory",
            )
    except Exception:
        pass

    logger.info(
        "history_compressed",
        chat_id=chat_id,
        compressed=len(to_compress),
        kept=len(to_keep),
    )


def _get_history(chat_id: str) -> list[dict]:
    """Return the reconstructed conversation history for a chat.

    Combines: optional pinned context + optional summary (as a synthetic exchange)
    + fresh verbatim messages. Expired sessions return empty.

    Args:
        chat_id: The Telegram chat ID.

    Returns:
        List of {"role", "content"} dicts ready to pass as prior_messages.
    """
    entry = _chat_history.get(chat_id)
    if not entry:
        return []
    if time.time() - entry["last_ts"] > _HISTORY_TTL_SECONDS:
        del _chat_history[chat_id]
        return []

    result: list[dict] = []

    # Pinned session context (/ctx) goes first — always present for the agent
    if entry.get("session_context"):
        result += [
            {"role": "user", "content": f"[Session context] {entry['session_context']}"},
            {"role": "assistant", "content": "Got it, I'll keep that in mind."},
        ]

    # Compressed summary of older turns (if any)
    if entry.get("summary"):
        result += [
            {"role": "user", "content": f"[Earlier in this session] {entry['summary']}"},
            {"role": "assistant", "content": "Understood."},
        ]

    # Fresh verbatim messages
    result += list(entry["messages"])
    return result


def _append_history(chat_id: str, role: str, content: str) -> None:
    """Append a message to the chat history.

    Args:
        chat_id: The Telegram chat ID.
        role: "user" or "assistant".
        content: Message text.
    """
    if chat_id not in _chat_history:
        _chat_history[chat_id] = {
            "messages": deque(maxlen=_HISTORY_MAX_FRESH),
            "last_ts": time.time(),
            "summary": None,
            "session_context": None,
            "session_tokens_in": 0,
            "session_tokens_out": 0,
            "cost_alerted": False,
        }
    _chat_history[chat_id]["messages"].append({"role": role, "content": content})
    _chat_history[chat_id]["last_ts"] = time.time()


def _set_session_context(chat_id: str, context: str) -> None:
    """Pin a persistent context note for the session.

    Args:
        chat_id: The Telegram chat ID.
        context: Context string to pin.
    """
    if chat_id not in _chat_history:
        _chat_history[chat_id] = {
            "messages": deque(maxlen=_HISTORY_MAX_FRESH),
            "last_ts": time.time(),
            "summary": None,
            "session_context": None,
            "session_tokens_in": 0,
            "session_tokens_out": 0,
            "cost_alerted": False,
        }
    _chat_history[chat_id]["session_context"] = context
    _chat_history[chat_id]["last_ts"] = time.time()


def _clear_history(chat_id: str) -> None:
    """Clear all history for a chat (session window reset).

    Args:
        chat_id: The Telegram chat ID.
    """
    _chat_history.pop(chat_id, None)


async def cmd_newchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset the conversation window — start fresh with no history or context.

    Use this when switching topics or when the agent seems confused by old context.

    Usage: /newchat
    """
    chat_id = str(update.effective_chat.id)
    _clear_history(chat_id)
    await update.message.reply_text("Session cleared. Fresh start.")
    logger.info("session_cleared", chat_id=chat_id)


async def cmd_ctx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pin a persistent context note for the current session.

    The context is prepended to every message you send in this session,
    so the agent always has it in mind. Useful for: setting a goal, describing
    the audience, giving background on a project, or pointing at a specific account.

    Usage: /ctx <text>
    Example: /ctx we're targeting devs building on Base, keep things technical
    Example: /ctx focus on mentorable.xyz today, here's the context: [paste]
    """
    if not context.args:
        chat_id = str(update.effective_chat.id)
        entry = _chat_history.get(chat_id)
        current = entry.get("session_context") if entry else None
        if current:
            await update.message.reply_text(f"Current session context:\n\n{current}\n\nUse /ctx <new text> to replace it, or /newchat to clear everything.")
        else:
            await update.message.reply_text("No session context set.\n\nUsage: /ctx <text>")
        return

    ctx_text = " ".join(context.args)
    chat_id = str(update.effective_chat.id)
    _set_session_context(chat_id, ctx_text)
    await update.message.reply_text(f"Context pinned for this session:\n\n{ctx_text}")
    logger.info("session_context_set", chat_id=chat_id)


async def handle_free_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free-form text messages using the autonomous agent.

    Passes rolling conversation history to the agent so it remembers
    what it said and did earlier in the same Telegram session.

    History management:
    - Fresh messages kept verbatim (last 4 turns)
    - Older turns compressed to a summary via Haiku before being discarded
    - Pinned session context (/ctx) always prepended
    - Reply-to messages injected as explicit context

    The autonomous agent has access to ALL registered skills as tools.
    Claude decides which tools to call based on the instruction.
    """
    # Only respond to the operator's chat
    if str(update.effective_chat.id) != settings.telegram_chat_id:
        return

    user_text = update.message.text
    if not user_text:
        return

    chat_id = str(update.effective_chat.id)
    await update.message.reply_text("On it...")

    try:
        from agent.autonomous import run_autonomous

        # If Wave is replying to a specific message, inject it as context.
        # This is the "tag" mechanic — reply to any message to make it part of the task.
        task = user_text
        replied = update.message.reply_to_message
        if replied and replied.text and replied.text != user_text:
            quoted = replied.text[:400].replace("\n", " ")
            task = f'[Referring to: "{quoted}"]\n\n{user_text}'

        # Get the active contract for context (default: astrlboy's own)
        contract = None
        try:
            async with async_session_factory() as session:
                result = await session.execute(
                    select(Contract).where(Contract.status == ContractStatus.ACTIVE).limit(1)
                )
                contract = result.scalar_one_or_none()
        except Exception:
            pass

        # Compress old turns before adding the new message — keeps the window lean
        await _maybe_compress_history(chat_id)

        # Retrieve history (summary + fresh) and append the incoming message
        prior_messages = _get_history(chat_id)
        _append_history(chat_id, "user", task)

        # Run the autonomous agent with all skills available
        agent_result = await run_autonomous(
            task=task,
            contract=contract,
            prior_messages=prior_messages,
        )

        # Clean markdown artifacts — agent should write plain text for Telegram
        # but strip any remaining markdown as a safety net
        import re

        response_text = agent_result.text or "Done (no text output)."
        response_text = re.sub(r"\*\*(.+?)\*\*", r"\1", response_text)  # **bold** → bold
        response_text = re.sub(r"__(.+?)__", r"\1", response_text)      # __bold__ → bold
        response_text = re.sub(r"\*(.+?)\*", r"\1", response_text)      # *italic* → italic
        response_text = re.sub(r"^#{1,6}\s+", "", response_text, flags=re.MULTILINE)  # ## headers
        response_text = re.sub(r"^[|].*[|]$", "", response_text, flags=re.MULTILINE)  # | tables |
        response_text = re.sub(r"^[-]{3,}$", "", response_text, flags=re.MULTILINE)   # ---
        response_text = re.sub(r"\n{3,}", "\n\n", response_text)        # collapse blank lines

        tool_summary = ""
        if agent_result.tool_calls:
            tools_used = list({tc["tool"] for tc in agent_result.tool_calls})
            tool_summary = f"\n\n[{agent_result.turns} turns | tools: {', '.join(tools_used)}]"

        full_response = response_text.strip() + tool_summary

        # Store agent response in history (without tool summary — that's display-only)
        _append_history(chat_id, "assistant", response_text.strip())

        # Accumulate session token costs and alert Wave if spend is getting high.
        # Sonnet 4.6: $3/MTok input, $15/MTok output.
        entry = _chat_history.get(chat_id)
        if entry and agent_result.input_tokens:
            entry["session_tokens_in"] += agent_result.input_tokens
            entry["session_tokens_out"] += agent_result.output_tokens
            session_cost_cents = (
                entry["session_tokens_in"] * 3 / 1_000_000 * 100 +
                entry["session_tokens_out"] * 15 / 1_000_000 * 100
            )
            if session_cost_cents >= _SESSION_COST_ALERT_CENTS and not entry["cost_alerted"]:
                entry["cost_alerted"] = True
                cost_str = f"${session_cost_cents / 100:.2f}"
                await update.message.reply_text(
                    f"heads up — this session has used {cost_str} in Claude tokens "
                    f"({entry['session_tokens_in']:,} in / {entry['session_tokens_out']:,} out). "
                    f"Send /newchat to start fresh and cut costs."
                )

        # Telegram max message length is 4096
        if len(full_response) <= 4096:
            await update.message.reply_text(full_response)
        else:
            # Split into chunks
            for i in range(0, len(full_response), 4096):
                await update.message.reply_text(full_response[i:i + 4096])

    except Exception as exc:
        logger.error("free_message_failed", error=str(exc))
        await update.message.reply_text(f"Error: {exc}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show available commands.

    Usage: /help
    """
    help_text = (
        "astrlboy commands\n\n"
        "Control\n"
        "  /pause — pause all activity\n"
        "  /resume — resume activity\n"
        "  /auto — auto mode (posts without approval)\n"
        "  /manual — manual mode (all posts need approval)\n\n"
        "Approvals\n"
        "  /pending — list pending approvals\n"
        "  /approve <id> — approve an interaction\n"
        "  /reject <id> — reject an interaction\n\n"
        "Actions\n"
        "  /trending [keywords] — search what's trending\n"
        "  /makepost [slug] — generate + post content now\n"
        "  /mentions — check and reply to mentions\n"
        "  /addcontract — onboard a new client\n\n"
        "Monitor\n"
        "  /status — agent status overview\n"
        "  /contracts — list contracts\n"
        "  /content — recent content\n"
        "  /trends — recent trend signals\n"
        "  /experiments — running experiments\n"
        "  /jobs — recent job applications\n"
        "  /escalations — unresolved escalations\n\n"
        "Session\n"
        "  /newchat — clear history, fresh start\n"
        "  /ctx <text> — pin context for this session (e.g. 'focus on Base devs')\n"
        "  /ctx — show current pinned context\n"
        "  Reply to any message — inject it as context for your next message\n\n"
        "Free-form (autonomous agent)\n"
        "  Just send any message — astrlboy has access to ALL\n"
        "  27 skills and decides what to do:\n"
        "  'write a tweet about AI agents'\n"
        "  'find accounts to follow in the AI space'\n"
        "  'research what people think about Claude'\n"
        "  'analyze trending content in crypto'\n"
        "  'check competitors and summarize changes'\n"
    )
    await update.message.reply_text(help_text)


# ── Bot setup ──────────────────────────────────────────────────────


def create_telegram_app():
    """Create and configure the Telegram bot application.

    Returns:
        A configured Telegram Application instance, or None if not configured.
    """
    if not settings.telegram_bot_token:
        logger.warning("telegram_bot_not_configured")
        return None

    app = ApplicationBuilder().token(settings.telegram_bot_token).build()

    # Commands
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("contracts", cmd_contracts))
    app.add_handler(CommandHandler("content", cmd_content))
    app.add_handler(CommandHandler("trends", cmd_trends))
    app.add_handler(CommandHandler("experiments", cmd_experiments))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("escalations", cmd_escalations))
    app.add_handler(CommandHandler("trending", cmd_trending))
    app.add_handler(CommandHandler("makepost", cmd_makepost))
    app.add_handler(CommandHandler("addcontract", cmd_addcontract))
    app.add_handler(CommandHandler("mentions", cmd_mentions))
    app.add_handler(CommandHandler("auto", cmd_auto))
    app.add_handler(CommandHandler("manual", cmd_manual))
    app.add_handler(CommandHandler("newchat", cmd_newchat))
    app.add_handler(CommandHandler("ctx", cmd_ctx))

    # Free-form messages — must be last so commands get priority
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_message))

    return app
