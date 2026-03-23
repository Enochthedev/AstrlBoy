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
from db.base import async_session_factory
from db.models.outbound_emails import OutboundEmail
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
        email_type: str = "general",
        contract_slug: str | None = None,
        **kwargs: Any,
    ) -> dict[str, str]:
        """Send an email via Resend HTTP API.

        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Email body (plain text or HTML).
            from_addr: Sender address. Defaults to agent_email from config.
            html: If True, body is treated as HTML.
            email_type: Category for tracking — 'application', 'briefing',
                'follow_up', or 'general'.
            contract_slug: Optional contract this email relates to.

        Returns:
            Dict with 'to', 'subject', 'status', and 'resend_id'.

        Raises:
            SkillExecutionError: If sending fails.
        """
        sender = from_addr or settings.agent_email
        api_key = settings.resend_api_key

        if not api_key:
            raise SkillExecutionError("RESEND_API_KEY not configured")

        # Auto-render plain text into HTML template for professional formatting.
        # If the caller already provides HTML, use it as-is.
        html_body = ""
        if html:
            html_body = body
        else:
            from core.email_templates import render_email

            html_body = render_email(
                email_type,
                subject=subject,
                body=body,
                contract_slug=contract_slug or "",
            )

        payload: dict[str, Any] = {
            "from": sender,
            "to": [to],
            "subject": subject,
            "html": html_body,
            # Include plain text fallback for clients that don't render HTML
            "text": body if not html else "",
        }

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

            # Persist to outbound_emails so the agent can see what it's sent
            try:
                async with async_session_factory() as session:
                    outbound = OutboundEmail(
                        resend_id=resend_id or None,
                        from_email=sender,
                        to_email=to,
                        subject=subject,
                        text_body=body,
                        html_body=html_body,
                        email_type=email_type,
                        contract_slug=contract_slug,
                    )
                    session.add(outbound)
                    await session.commit()
            except Exception as db_exc:
                # Non-fatal — email was already sent, just log the tracking failure
                logger.warning("outbound_email_store_failed", error=str(db_exc))

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
