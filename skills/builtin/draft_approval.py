"""
Telegram draft approval skill.

Sends drafted content/replies to Wave via Telegram for approval.
Creates an Interaction record if one doesn't exist, so the approve/reject
commands can look it up and post it.
"""

from typing import Any

from telegram import Bot

from core.config import settings
from core.constants import InteractionStatus
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from db.base import async_session_factory
from db.models.interactions import Interaction
from skills.base import BaseTool

logger = get_logger("skills.draft_approval")


class DraftApprovalSkill(BaseTool):
    """Send a draft to the Telegram approval queue."""

    name = "draft_approval"
    description = "Send a draft to Wave via Telegram for approval before posting."
    version = "1.1.0"

    def __init__(self) -> None:
        self._bot = Bot(token=settings.telegram_bot_token)

    async def execute(
        self,
        draft: str,
        platform: str = "x",
        interaction_id: str | None = None,
        contract_slug: str = "",
        title: str = "",
        thread_context: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        """Send a draft for approval.

        If no interaction_id is provided, creates an Interaction record
        so the /approve command can find and post it.

        Args:
            draft: The draft text to approve.
            platform: Platform the draft is for.
            interaction_id: Existing interaction UUID (optional).
            contract_slug: Client slug for context.
            title: Title/label for the draft.
            thread_context: Context about the thread being replied to.

        Returns:
            Dict with 'status', 'message_id', and 'interaction_id'.

        Raises:
            SkillExecutionError: If sending fails.
        """
        try:
            # Create an Interaction record if none exists
            if not interaction_id:
                async with async_session_factory() as session:
                    interaction = Interaction(
                        platform=platform,
                        draft=draft,
                        status=InteractionStatus.PENDING,
                        thread_context=thread_context or f"[{contract_slug}] {title}",
                    )
                    session.add(interaction)
                    await session.commit()
                    interaction_id = str(interaction.id)

            label = title or contract_slug or platform
            message_text = (
                f"Approval Request ({label})\n\n"
                f"{f'Context: {thread_context[:300]}' + chr(10) + chr(10) if thread_context else ''}"
                f"Draft:\n{draft}\n\n"
                f"/approve {interaction_id}\n"
                f"/reject {interaction_id}"
            )

            msg = await self._bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=message_text,
            )

            logger.info(
                "approval_sent",
                interaction_id=interaction_id,
                platform=platform,
            )
            return {
                "status": "pending",
                "message_id": str(msg.message_id),
                "interaction_id": interaction_id,
            }
        except Exception as exc:
            logger.error("draft_approval_failed", error=str(exc))
            raise SkillExecutionError(f"Telegram approval send failed: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for draft_approval inputs."""
        return {
            "type": "object",
            "properties": {
                "draft": {"type": "string", "description": "Draft text to approve"},
                "platform": {"type": "string", "description": "Target platform"},
                "interaction_id": {"type": "string", "description": "Existing interaction UUID (optional)"},
                "contract_slug": {"type": "string", "description": "Client slug for context"},
                "title": {"type": "string", "description": "Title/label for the draft"},
                "thread_context": {"type": "string", "description": "Thread context"},
            },
            "required": ["draft"],
        }
