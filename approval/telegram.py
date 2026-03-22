"""
Telegram bot for the operator approval queue.

Wave can approve/reject drafts, pause/resume the agent, and check status.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from core.config import settings
from core.constants import InteractionStatus
from core.logging import get_logger
from db.base import async_session_factory
from db.models.interactions import Interaction

logger = get_logger("approval.telegram")


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


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show agent status including pending queue count.

    Usage: /status
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(Interaction).where(Interaction.status == InteractionStatus.PENDING)
        )
        pending = result.scalars().all()

    status_text = (
        f"Agent: {'PAUSED' if settings.agent_paused else 'RUNNING'}\n"
        f"Pending approvals: {len(pending)}\n"
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
        lines.append(
            f"[{interaction.platform}] {interaction.draft[:100]}...\n"
            f"/approve {interaction.id}\n/reject {interaction.id}\n"
        )
    await update.message.reply_text("\n---\n".join(lines))


def create_telegram_app():
    """Create and configure the Telegram bot application.

    Returns:
        A configured Telegram Application instance.
    """
    if not settings.telegram_bot_token:
        logger.warning("telegram_bot_not_configured")
        return None

    app = ApplicationBuilder().token(settings.telegram_bot_token).build()

    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pending", cmd_pending))

    return app
