"""
Resend email delivery status checker.

Checks the delivery status of a sent email via the Resend API.
Use this to verify whether job applications, briefings, or follow-ups
were actually delivered, bounced, or opened.

Resend tracks: queued → sent → delivered → opened → clicked → bounced.
"""

from typing import Any

import httpx

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.check_email_status")

_RESEND_API_BASE = "https://api.resend.com"


class CheckEmailStatusSkill(BaseTool):
    """Check delivery status of a sent email via Resend API.

    Queries the Resend API for the current status of an email by its
    Resend ID (returned by send_email). Reports delivery state, bounces,
    opens, and clicks.
    """

    name = "check_email_status"
    description = (
        "Check the delivery status of a sent email. Takes a Resend email ID "
        "and returns delivery status (queued/sent/delivered/bounced/opened)."
    )
    version = "1.0.0"

    async def execute(
        self,
        email_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Check email delivery status.

        Args:
            email_id: The Resend email ID (returned by send_email as 'resend_id').

        Returns:
            Dict with 'id', 'status', 'to', 'subject', 'created_at',
            and 'last_event' (most recent delivery event).

        Raises:
            SkillExecutionError: If the API call fails.
        """
        api_key = settings.resend_api_key
        if not api_key:
            raise SkillExecutionError("RESEND_API_KEY not configured")

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{_RESEND_API_BASE}/emails/{email_id}",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            result = {
                "id": data.get("id", email_id),
                "status": data.get("last_event", "unknown"),
                "to": data.get("to", []),
                "subject": data.get("subject", ""),
                "from": data.get("from", ""),
                "created_at": data.get("created_at", ""),
            }

            logger.info(
                "email_status_checked",
                email_id=email_id,
                status=result["status"],
            )
            return result

        except httpx.HTTPStatusError as exc:
            error_body = exc.response.text
            logger.error(
                "email_status_check_failed",
                email_id=email_id,
                status=exc.response.status_code,
                error=error_body,
            )
            raise SkillExecutionError(
                f"Email status check failed: {exc.response.status_code} {error_body}"
            ) from exc
        except Exception as exc:
            logger.error("email_status_check_failed", email_id=email_id, error=str(exc))
            raise SkillExecutionError(f"Email status check failed: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for check_email_status inputs."""
        return {
            "type": "object",
            "properties": {
                "email_id": {
                    "type": "string",
                    "description": "Resend email ID (from send_email's 'resend_id' field)",
                },
            },
            "required": ["email_id"],
        }
