"""
SMTP email sending skill via Resend.

Used for sending job applications from agent@astrlboy.xyz,
delivering weekly briefings, and any outbound email.
"""

from email.message import EmailMessage
from typing import Any

import aiosmtplib

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.send_email")


class SendEmailSkill(BaseTool):
    """Send an email via SMTP (Resend)."""

    name = "send_email"
    description = "Send an email via SMTP. Used for applications, briefings, and outbound comms."
    version = "1.0.0"

    async def execute(
        self,
        to: str,
        subject: str,
        body: str,
        from_addr: str | None = None,
        html: bool = False,
        **kwargs: Any,
    ) -> dict[str, str]:
        """Send an email.

        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Email body (plain text or HTML).
            from_addr: Sender address. Defaults to agent_email from config.
            html: If True, body is treated as HTML.

        Returns:
            Dict with 'to', 'subject', and 'status'.

        Raises:
            SkillExecutionError: If sending fails.
        """
        try:
            msg = EmailMessage()
            msg["From"] = from_addr or settings.agent_email
            msg["To"] = to
            msg["Subject"] = subject

            if html:
                msg.set_content("Plain text version not available.")
                msg.add_alternative(body, subtype="html")
            else:
                msg.set_content(body)

            await aiosmtplib.send(
                msg,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user,
                password=settings.smtp_pass,
                start_tls=True,
            )

            logger.info("email_sent", to=to, subject=subject)
            return {"to": to, "subject": subject, "status": "sent"}
        except Exception as exc:
            logger.error("send_email_failed", to=to, error=str(exc))
            raise SkillExecutionError(f"Email send failed to {to}: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for send_email inputs."""
        return {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email"},
                "subject": {"type": "string", "description": "Subject line"},
                "body": {"type": "string", "description": "Email body"},
                "from_addr": {"type": "string", "description": "Sender address (optional)"},
                "html": {"type": "boolean", "default": False},
            },
            "required": ["to", "subject", "body"],
        }
