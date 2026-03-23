"""
IMAP email reader skill.

Reads incoming emails from the agent inbox. Used to check for
job application replies and client communications.
"""

import email
import imaplib
from typing import Any

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.read_email")


class ReadEmailSkill(BaseTool):
    """Read emails from the agent inbox via IMAP."""

    name = "read_email"
    description = "Read incoming emails from the agent inbox. Check for replies and new messages."
    version = "1.0.0"

    async def execute(
        self,
        folder: str = "INBOX",
        unseen_only: bool = True,
        limit: int = 10,
        **kwargs: Any,
    ) -> list[dict[str, str]]:
        """Read emails from the inbox.

        Args:
            folder: IMAP folder to read from.
            unseen_only: If True, only return unread messages.
            limit: Maximum number of messages to return.

        Returns:
            List of dicts with 'from', 'subject', 'date', 'body' keys.

        Raises:
            SkillExecutionError: If IMAP is not configured or reading fails.
        """
        # Skip if IMAP credentials aren't configured — inbound email may be
        # handled by the Resend webhook instead
        if not settings.imap_host or not settings.imap_pass:
            logger.info("read_email_skipped", reason="IMAP not configured, using Resend webhook for inbound")
            return []

        try:
            # IMAP is synchronous — acceptable here since it's called from a scheduled job
            mail = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
            mail.login(settings.imap_user, settings.imap_pass)
            mail.select(folder)

            criteria = "UNSEEN" if unseen_only else "ALL"
            _, message_numbers = mail.search(None, criteria)

            messages = []
            msg_nums = message_numbers[0].split()[-limit:] if message_numbers[0] else []

            for num in msg_nums:
                _, msg_data = mail.fetch(num, "(RFC822)")
                if msg_data[0] is None:
                    continue
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                            break
                else:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="replace")

                messages.append({
                    "from": msg.get("From", ""),
                    "subject": msg.get("Subject", ""),
                    "date": msg.get("Date", ""),
                    "body": body,
                })

            mail.logout()
            logger.info("emails_read", count=len(messages), folder=folder)
            return messages
        except Exception as exc:
            logger.error("read_email_failed", error=str(exc))
            raise SkillExecutionError(f"Email read failed: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for read_email inputs."""
        return {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "default": "INBOX"},
                "unseen_only": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "default": 10},
            },
            "required": [],
        }
