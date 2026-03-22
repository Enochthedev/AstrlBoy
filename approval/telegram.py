"""
Telegram bot for the operator approval queue and monitoring.

Wave can approve/reject drafts, pause/resume the agent, check status,
and monitor contracts, content, trends, and escalations.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

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
            await session.commit()

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

        status_text = (
            f"{'PAUSED' if settings.agent_paused else 'RUNNING'}\n"
            f"Active contracts: {active_contracts}\n"
            f"Pending approvals: {pending}\n"
            f"Unresolved escalations: {unresolved}"
        )
    except Exception:
        status_text = (
            f"{'PAUSED' if settings.agent_paused else 'RUNNING'}\n"
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


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show available commands.

    Usage: /help
    """
    help_text = (
        "astrlboy commands\n\n"
        "Control\n"
        "  /pause — pause all activity\n"
        "  /resume — resume activity\n\n"
        "Approvals\n"
        "  /pending — list pending approvals\n"
        "  /approve <id> — approve an interaction\n"
        "  /reject <id> — reject an interaction\n\n"
        "Monitor\n"
        "  /status — agent status overview\n"
        "  /contracts — list contracts\n"
        "  /content — recent content\n"
        "  /trends — recent trend signals\n"
        "  /experiments — running experiments\n"
        "  /jobs — recent job applications\n"
        "  /escalations — unresolved escalations\n"
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

    return app
