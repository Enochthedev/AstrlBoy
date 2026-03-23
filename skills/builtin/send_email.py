"""
Email sending skill via Resend HTTP API.

Railway blocks outbound SMTP ports for hobby users, so we call the
Resend REST API directly via httpx. The RESEND_API_KEY is the same
key that was previously used as SMTP_PASS — Resend uses one key
for both SMTP and HTTP API access.
"""

from typing import Any

import httpx

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.send_email")

_RESEND_API_URL = "https://api.resend.com/emails"


class SendEmailSkill(BaseTool):
    """Send an email via Resend HTTP API."""

    name = "send_email"
    description = "Send an email via Resend. Used for applications, briefings, and outbound comms."
    version = "2.0.0"

    async def execute(
        self,
        to: str,
        subject: str,
        body: str,
        from_addr: str | None = None,
        html: bool = False,
        **kwargs: Any,
    ) -> dict[str, str]:
        """Send an email via Resend HTTP API.

        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Email body (plain text or HTML).
            from_addr: Sender address. Defaults to agent_email from config.
            html: If True, body is treated as HTML.

        Returns:
            Dict with 'to', 'subject', 'status', and 'resend_id'.

        Raises:
            SkillExecutionError: If sending fails.
        """
        sender = from_addr or settings.agent_email
        api_key = settings.resend_api_key

        if not api_key:
            raise SkillExecutionError("RESEND_API_KEY not configured")

        payload: dict[str, Any] = {
            "from": sender,
            "to": [to],
            "subject": subject,
        }
        if html:
            payload["html"] = body
        else:
            payload["text"] = body

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    _RESEND_API_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

            resend_id = data.get("id", "")
            logger.info("email_sent", to=to, subject=subject, resend_id=resend_id)
            return {"to": to, "subject": subject, "status": "sent", "resend_id": resend_id}

        except httpx.HTTPStatusError as exc:
            error_body = exc.response.text
            logger.error("send_email_failed", to=to, status=exc.response.status_code, error=error_body)
            raise SkillExecutionError(f"Email send failed to {to}: {exc.response.status_code} {error_body}") from exc
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
