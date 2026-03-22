"""
Telegram draft approval skill.

Sends drafted content/replies to Wave via Telegram for approval.
Used for Reddit and Discord posts that require human review before posting.
"""

from typing import Any
from uuid import UUID

from telegram import Bot

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.draft_approval")


class DraftApprovalSkill(BaseTool):
    """Send a draft to the Telegram approval queue."""

    name = "draft_approval"
    description = "Send a draft to Wave via Telegram for approval. Use for Reddit/Discord posts."
    version = "1.0.0"

    def __init__(self) -> None:
        self._bot = Bot(token=settings.telegram_bot_token)

    async def execute(
        self,
        interaction_id: str,
        platform: str,
        draft: str,
        thread_context: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        """Send a draft for approval.

        Args:
            interaction_id: UUID of the interaction in the DB.
            platform: Platform the draft is for (e.g. 'reddit', 'discord').
            draft: The draft text to approve.
            thread_context: Context about the thread being replied to.

        Returns:
            Dict with 'status' and 'message_id'.

        Raises:
            SkillExecutionError: If sending fails.
        """
        try:
            message_text = (
                f"**Approval Request** ({platform})\n\n"
                f"**Context:** {thread_context[:500]}\n\n"
                f"**Draft:**\n{draft}\n\n"
                f"/approve {interaction_id}\n"
                f"/reject {interaction_id}"
            )

            msg = await self._bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=message_text,
                parse_mode="Markdown",
            )

            logger.info(
                "approval_sent",
                interaction_id=interaction_id,
                platform=platform,
            )
            return {"status": "pending", "message_id": str(msg.message_id)}
        except Exception as exc:
            logger.error("draft_approval_failed", error=str(exc))
            raise SkillExecutionError(f"Telegram approval send failed: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for draft_approval inputs."""
        return {
            "type": "object",
            "properties": {
                "interaction_id": {"type": "string", "description": "Interaction UUID"},
                "platform": {"type": "string", "description": "Target platform"},
                "draft": {"type": "string", "description": "Draft text to approve"},
                "thread_context": {"type": "string", "description": "Thread context"},
            },
            "required": ["interaction_id", "platform", "draft"],
        }
