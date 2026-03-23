"""
Email reader skill — queries inbound emails stored by the Resend webhook.

Inbound emails arrive via Resend webhook → /webhooks/inbound/email → DB.
This skill queries the inbound_emails table so the agent can search
through past emails, find replies to job applications, or check for
new messages at any time.
"""

from typing import Any

from sqlalchemy import select

from core.exceptions import SkillExecutionError
from core.logging import get_logger
from db.base import async_session_factory
from db.models.inbound_emails import InboundEmail
from skills.base import BaseTool

logger = get_logger("skills.read_email")


class ReadEmailSkill(BaseTool):
    """Read inbound emails from the database (stored by Resend webhook)."""

    name = "read_email"
    description = (
        "Read incoming emails from the agent inbox. Queries emails stored "
        "by the Resend webhook. Can filter by unread, sender, or search term."
    )
    version = "2.0.0"

    async def execute(
        self,
        unread_only: bool = True,
        from_email: str | None = None,
        search: str | None = None,
        limit: int = 10,
        mark_read: bool = True,
        **kwargs: Any,
    ) -> list[dict[str, str]]:
        """Read emails from the inbound_emails table.

        Args:
            unread_only: If True, only return unread messages.
            from_email: Filter by sender email address (partial match).
            search: Search term to match against subject or body.
            limit: Maximum number of messages to return.
            mark_read: If True, mark returned emails as read.

        Returns:
            List of dicts with 'id', 'from', 'to', 'subject', 'date',
            'body', and 'is_read' keys.

        Raises:
            SkillExecutionError: If the database query fails.
        """
        try:
            async with async_session_factory() as session:
                query = select(InboundEmail).order_by(
                    InboundEmail.received_at.desc()
                )

                if unread_only:
                    query = query.where(InboundEmail.is_read == False)  # noqa: E712

                if from_email:
                    query = query.where(
                        InboundEmail.from_email.ilike(f"%{from_email}%")
                    )

                if search:
                    query = query.where(
                        InboundEmail.subject.ilike(f"%{search}%")
                        | InboundEmail.text_body.ilike(f"%{search}%")
                    )

                query = query.limit(limit)
                result = await session.execute(query)
                emails = result.scalars().all()

                messages = []
                for em in emails:
                    body = em.text_body or em.html_body or ""
                    messages.append({
                        "id": str(em.id),
                        "from": em.from_email,
                        "to": em.to_email,
                        "subject": em.subject,
                        "date": em.received_at.isoformat() if em.received_at else "",
                        "body": body,
                        "is_read": em.is_read,
                    })

                # Mark as read so the agent doesn't re-process them
                if mark_read and emails:
                    for em in emails:
                        if not em.is_read:
                            em.is_read = True
                    await session.commit()

                logger.info(
                    "emails_read",
                    count=len(messages),
                    unread_only=unread_only,
                    search=search,
                )
                return messages

        except Exception as exc:
            logger.error("read_email_failed", error=str(exc))
            raise SkillExecutionError(f"Email read failed: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for read_email inputs."""
        return {
            "type": "object",
            "properties": {
                "unread_only": {
                    "type": "boolean",
                    "default": True,
                    "description": "Only return unread emails",
                },
                "from_email": {
                    "type": "string",
                    "description": "Filter by sender (partial match)",
                },
                "search": {
                    "type": "string",
                    "description": "Search subject and body text",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max emails to return",
                },
                "mark_read": {
                    "type": "boolean",
                    "default": True,
                    "description": "Mark returned emails as read",
                },
            },
            "required": [],
        }
