"""
Central agent orchestration service.

Handles pause state, escalation to the operator, and structured action logging.
All modules call this for escalation and state checks.
"""

from uuid import UUID, uuid4

from sqlalchemy import select

from core.config import settings
from core.logging import get_logger
from db.base import async_session_factory
from db.models.escalations import Escalation

logger = get_logger("agent.service")


class AgentService:
    """Central orchestration layer for astrlboy.

    Provides pause checking, escalation to Wave, and action logging.
    """

    async def is_paused(self) -> bool:
        """Check if the agent is currently paused.

        Returns:
            True if AGENT_PAUSED is set to true.
        """
        return settings.agent_paused

    async def escalate(self, reason: str, context: dict) -> Escalation:
        """Escalate an issue to the operator.

        Creates an escalation record in the DB and sends a Telegram
        notification to Wave.

        Args:
            reason: Why the escalation is needed.
            context: Additional context (entity IDs, error details, etc).

        Returns:
            The created Escalation record.
        """
        async with async_session_factory() as session:
            escalation = Escalation(
                reason=reason,
                context=context,
            )
            session.add(escalation)
            await session.commit()
            await session.refresh(escalation)

        logger.warning(
            "escalation_created",
            escalation_id=str(escalation.id),
            reason=reason,
        )

        # Send Telegram notification
        try:
            from telegram import Bot

            bot = Bot(token=settings.telegram_bot_token)
            await bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=(
                    f"**Escalation**\n\n"
                    f"**Reason:** {reason}\n\n"
                    f"**Context:** {context}\n\n"
                    f"ID: {escalation.id}"
                ),
                parse_mode="Markdown",
            )
        except Exception as exc:
            # Telegram failure shouldn't block the escalation record
            logger.error("telegram_escalation_failed", error=str(exc))

        return escalation

    async def log_action(
        self,
        entity_type: str,
        entity_id: UUID,
        action: str,
        outcome: str,
        contract_slug: str = "",
        duration_ms: int = 0,
    ) -> None:
        """Log a structured action to the logger.

        Args:
            entity_type: Type of entity (e.g. 'content', 'interaction').
            entity_id: UUID of the entity.
            action: What was done (e.g. 'self_critique', 'publish').
            outcome: Result (e.g. 'approved', 'rejected').
            contract_slug: Client slug for context.
            duration_ms: How long the action took.
        """
        logger.info(
            "agent_action",
            entity_type=entity_type,
            entity_id=str(entity_id),
            action=action,
            outcome=outcome,
            contract_slug=contract_slug,
            duration_ms=duration_ms,
        )

    async def get_pending_escalations(self) -> list[Escalation]:
        """Return all unresolved escalations.

        Returns:
            List of Escalation records where resolved is False.
        """
        async with async_session_factory() as session:
            result = await session.execute(
                select(Escalation).where(Escalation.resolved == False)  # noqa: E712
            )
            return list(result.scalars().all())

    async def resolve_escalation(self, escalation_id: UUID) -> Escalation:
        """Mark an escalation as resolved.

        Args:
            escalation_id: The escalation to resolve.

        Returns:
            The updated Escalation record.
        """
        from datetime import datetime, timezone

        async with async_session_factory() as session:
            result = await session.execute(
                select(Escalation).where(Escalation.id == escalation_id)
            )
            escalation = result.scalar_one()
            escalation.resolved = True
            escalation.resolved_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(escalation)

        logger.info("escalation_resolved", escalation_id=str(escalation_id))
        return escalation


# Singleton
agent_service = AgentService()
