"""
Telegram bot for the operator approval queue and monitoring.

Wave can approve/reject drafts, pause/resume the agent, check status,
and monitor contracts, content, trends, and escalations.
"""

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


# ── Approval commands ──────────────────────────────────────────────


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve a pending interaction and post it immediately.

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
            await session.commit()

        # Post the approved content
        posted = False
        if draft_text and platform:
            try:
                from skills.registry import skill_registry

                skill_name = f"post_{platform}"
                if await skill_registry.is_available(skill_name):
                    skill = await skill_registry.get(skill_name)
                    result = await skill.execute(text=draft_text[:280] if platform == "x" else draft_text)
                    posted = True
                    tweet_id = result.get("tweet_id", "")
                    await update.message.reply_text(
                        f"Approved & posted!\n\n{draft_text[:200]}\n\nTweet ID: {tweet_id}"
                    )
            except Exception as exc:
                logger.error("post_after_approve_failed", error=str(exc))

        if not posted:
            await update.message.reply_text(f"Approved: {interaction_id}")
        logger.info("interaction_approved", interaction_id=interaction_id)
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


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
        for r in results:
            title = r.get("title", "")[:80]
            snippet = r.get("content", "")[:100]
            lines.append(f"{title}\n{snippet}...")

        await update.message.reply_text("\n---\n".join(lines))
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
    """Check recent mentions and reply to them.

    Usage: /mentions
    """
    await update.message.reply_text("Checking mentions...")

    try:
        from skills.registry import skill_registry

        if not await skill_registry.is_available("get_mentions"):
            await update.message.reply_text("Mentions skill not available.")
            return

        mentions_skill = await skill_registry.get("get_mentions")
        mentions = await mentions_skill.execute(max_results=5)

        if not mentions:
            await update.message.reply_text("No recent mentions.")
            return

        # Show mentions and generate replies
        lines = []
        for m in mentions:
            lines.append(f"@{m['author_username']}: {m['text'][:120]}")

        await update.message.reply_text("Recent mentions:\n\n" + "\n---\n".join(lines))

        # Generate and post replies
        if not await skill_registry.is_available("post_x"):
            return

        post_skill = await skill_registry.get("post_x")
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)

        replied = 0
        pending = 0
        for m in mentions[:3]:  # reply to top 3
            try:
                response = await _client.messages.create(
                    model="claude-sonnet-4-5-20250514",
                    max_tokens=280,
                    system=(
                        "You are astrlboy, an AI personality on X. Write a reply to this mention.\n"
                        "Be sharp, concise, human. No AI slop. Max 280 chars.\n"
                        "If someone is asking a question, answer it. If they're making a point, engage with it."
                    ),
                    messages=[{"role": "user", "content": f"@{m['author_username']} said: {m['text']}"}],
                )
                reply_text = response.content[0].text[:280]

                if settings.agent_auto:
                    await post_skill.execute(text=reply_text, reply_to_id=m["id"])
                    replied += 1
                else:
                    # Manual mode — save for approval
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
                            f"Reply to @{m['author_username']}:\n\n{reply_text}\n\n"
                            f"/approve {interaction.id}\n/reject {interaction.id}"
                        )
            except Exception as exc:
                logger.warning("mention_reply_failed", mention_id=m["id"], error=str(exc))

        total = min(len(mentions), 3)
        if settings.agent_auto:
            await update.message.reply_text(f"Replied to {replied}/{total} mentions.")
        else:
            await update.message.reply_text(f"Sent {pending}/{total} replies for approval.")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def handle_free_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free-form text messages using the autonomous agent.

    The autonomous agent has access to ALL registered skills as tools.
    Claude decides which tools to call based on the instruction.

    Examples:
    - "write a tweet about AI agents taking over" → searches trends, drafts, posts
    - "what's trending in crypto?" → uses search + analyze_trending_content
    - "find accounts to follow in the AI space" → uses find_relevant_accounts + follow_x
    - "research what people think about Claude" → uses extract_sentiment + research_topic
    """
    # Only respond to the operator's chat
    if str(update.effective_chat.id) != settings.telegram_chat_id:
        return

    user_text = update.message.text
    if not user_text:
        return

    await update.message.reply_text("On it...")

    try:
        from agent.autonomous import run_autonomous

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

        # Run the autonomous agent with all skills available
        agent_result = await run_autonomous(
            task=user_text,
            contract=contract,
        )

        # Send result back — split long messages
        response_text = agent_result.text or "Done (no text output)."
        tool_summary = ""
        if agent_result.tool_calls:
            tools_used = list({tc["tool"] for tc in agent_result.tool_calls})
            tool_summary = f"\n\n[{agent_result.turns} turns | tools: {', '.join(tools_used)}]"

        full_response = response_text + tool_summary

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

    # Free-form messages — must be last so commands get priority
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_message))

    return app
