"""
Unified email listing skill — shows both sent and received emails.

Queries both inbound_emails and outbound_emails tables so the agent has
a complete view of all email activity. Supports filtering by direction,
contact address, search term, and conversation threading (grouping by
subject + contact to see the full back-and-forth).
"""

from typing import Any

from sqlalchemy import select, union_all
from sqlalchemy.sql import literal_column

from core.exceptions import SkillExecutionError
from core.logging import get_logger
from db.base import async_session_factory
from db.models.inbound_emails import InboundEmail
from db.models.outbound_emails import OutboundEmail
from skills.base import BaseTool

logger = get_logger("skills.list_emails")


class ListEmailsSkill(BaseTool):
    """List both sent and received emails with conversation threading."""

    name = "list_emails"
    description = (
        "List all emails — both sent and received. Can filter by direction "
        "(inbound/outbound), contact address, or search term. Supports "
        "conversation mode to see full email threads."
    )
    version = "1.0.0"

    async def execute(
        self,
        direction: str | None = None,
        contact: str | None = None,
        search: str | None = None,
        unread_only: bool = False,
        conversation: bool = False,
        limit: int = 20,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """List emails from both inbound and outbound tables.

        Args:
            direction: 'inbound', 'outbound', or None for both.
            contact: Filter by contact email address (partial match).
                For inbound: matches from_email. For outbound: matches to_email.
            search: Search term to match against subject or body.
            unread_only: If True, only return unread inbound emails
                (outbound are always "read").
            conversation: If True, group emails into conversation threads
                by normalizing subjects (strip Re:/Fwd:) and contact address.
            limit: Maximum number of emails/threads to return.

        Returns:
            List of email dicts. In conversation mode, each dict has a
            'messages' list showing the full thread ordered by date.

        Raises:
            SkillExecutionError: If the database query fails.
        """
        try:
            async with async_session_factory() as session:
                emails: list[dict[str, Any]] = []

                # Fetch inbound emails
                if direction != "outbound":
                    inbound_q = select(InboundEmail).order_by(
                        InboundEmail.received_at.desc()
                    )
                    if unread_only:
                        inbound_q = inbound_q.where(InboundEmail.is_read == False)  # noqa: E712
                    if contact:
                        inbound_q = inbound_q.where(
                            InboundEmail.from_email.ilike(f"%{contact}%")
                        )
                    if search:
                        inbound_q = inbound_q.where(
                            InboundEmail.subject.ilike(f"%{search}%")
                            | InboundEmail.text_body.ilike(f"%{search}%")
                        )
                    inbound_q = inbound_q.limit(limit)
                    result = await session.execute(inbound_q)
                    for em in result.scalars().all():
                        emails.append({
                            "id": str(em.id),
                            "direction": "inbound",
                            "from": em.from_email,
                            "to": em.to_email,
                            "subject": em.subject,
                            "body": em.text_body or em.html_body or "",
                            "date": em.received_at.isoformat() if em.received_at else "",
                            "is_read": em.is_read,
                            "contact": em.from_email,
                        })

                # Fetch outbound emails
                if direction != "inbound":
                    outbound_q = select(OutboundEmail).order_by(
                        OutboundEmail.sent_at.desc()
                    )
                    if contact:
                        outbound_q = outbound_q.where(
                            OutboundEmail.to_email.ilike(f"%{contact}%")
                        )
                    if search:
                        outbound_q = outbound_q.where(
                            OutboundEmail.subject.ilike(f"%{search}%")
                            | OutboundEmail.text_body.ilike(f"%{search}%")
                        )
                    outbound_q = outbound_q.limit(limit)
                    result = await session.execute(outbound_q)
                    for em in result.scalars().all():
                        emails.append({
                            "id": str(em.id),
                            "direction": "outbound",
                            "from": em.from_email,
                            "to": em.to_email,
                            "subject": em.subject,
                            "body": em.text_body or em.html_body or "",
                            "date": em.sent_at.isoformat() if em.sent_at else "",
                            "is_read": True,
                            "contact": em.to_email,
                            "email_type": em.email_type,
                        })

                # Sort all emails by date descending
                emails.sort(key=lambda e: e.get("date", ""), reverse=True)

                if not conversation:
                    logger.info(
                        "emails_listed",
                        count=len(emails[:limit]),
                        direction=direction or "all",
                    )
                    return emails[:limit]

                # Group into conversation threads by normalized subject + contact
                threads: dict[str, list[dict[str, Any]]] = {}
                for em in emails:
                    subject_norm = _normalize_subject(em["subject"])
                    contact_addr = em["contact"]
                    thread_key = f"{contact_addr}::{subject_norm}".lower()

                    if thread_key not in threads:
                        threads[thread_key] = []
                    threads[thread_key].append(em)

                # Sort threads by most recent message, then sort messages within
                # each thread chronologically (oldest first for reading order)
                result_threads: list[dict[str, Any]] = []
                for key, messages in threads.items():
                    messages.sort(key=lambda e: e.get("date", ""))
                    latest = messages[-1]
                    result_threads.append({
                        "thread_subject": latest["subject"],
                        "contact": latest["contact"],
                        "message_count": len(messages),
                        "latest_date": latest["date"],
                        "has_unread": any(
                            not m["is_read"] for m in messages
                            if m["direction"] == "inbound"
                        ),
                        "messages": messages,
                    })

                result_threads.sort(
                    key=lambda t: t["latest_date"], reverse=True
                )

                logger.info(
                    "email_threads_listed",
                    threads=len(result_threads[:limit]),
                    total_emails=len(emails),
                )
                return result_threads[:limit]

        except Exception as exc:
            logger.error("list_emails_failed", error=str(exc))
            raise SkillExecutionError(f"Email list failed: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for list_emails inputs."""
        return {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["inbound", "outbound"],
                    "description": "Filter by direction. Omit for both.",
                },
                "contact": {
                    "type": "string",
                    "description": "Filter by contact email (partial match)",
                },
                "search": {
                    "type": "string",
                    "description": "Search subject and body text",
                },
                "unread_only": {
                    "type": "boolean",
                    "default": False,
                    "description": "Only show unread inbound emails",
                },
                "conversation": {
                    "type": "boolean",
                    "default": False,
                    "description": "Group emails into conversation threads",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Max results to return",
                },
            },
            "required": [],
        }


def _normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd:/FW: prefixes for thread grouping.

    Args:
        subject: Raw email subject line.

    Returns:
        Normalized subject without reply/forward prefixes.
    """
    import re

    cleaned = re.sub(r"^(re|fwd|fw)\s*:\s*", "", subject.strip(), flags=re.IGNORECASE)
    # Handle nested prefixes like "Re: Re: Fwd: subject"
    while cleaned != subject:
        subject = cleaned
        cleaned = re.sub(r"^(re|fwd|fw)\s*:\s*", "", subject.strip(), flags=re.IGNORECASE)
    return cleaned.strip()
