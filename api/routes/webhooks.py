"""
Inbound webhook routes — Resend email ingestion.

Resend fires a POST to /webhooks/inbound/email with email metadata
when someone sends to agent@astrlboy.xyz. The webhook only includes
metadata (from, to, subject) — we call the Resend API to fetch the
actual body, then persist to the inbound_emails table, match against
job applications, and notify Wave.
"""

import hashlib
import hmac
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from core.config import settings
from core.logging import get_logger
from db.base import async_session_factory
from db.models.inbound_emails import InboundEmail
from db.models.job_applications import JobApplication

logger = get_logger("api.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify_resend_signature(
    payload: bytes, signature: str | None, secret: str
) -> bool:
    """Verify the Resend webhook HMAC-SHA256 signature.

    Args:
        payload: Raw request body bytes.
        signature: The svix-signature header value from Resend.
        secret: The webhook signing secret from Resend dashboard.

    Returns:
        True if signature is valid.
    """
    if not signature or not secret:
        return False

    # Resend uses svix — signature format: "v1,<base64_hmac>"
    try:
        expected = hmac.new(
            secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        for sig_part in signature.split(" "):
            tag, _, value = sig_part.partition(",")
            if tag == "v1" and hmac.compare_digest(expected, value):
                return True
    except Exception:
        pass
    return False


async def _fetch_email_body(email_id: str) -> tuple[str, str]:
    """Fetch the full email body from the Resend Received Emails API.

    Webhook payloads only include metadata — the body must be fetched separately.

    Args:
        email_id: The Resend email ID from the webhook payload.

    Returns:
        Tuple of (text_body, html_body). Either may be empty.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.resend.com/emails/receiving/{email_id}",
                headers={"Authorization": f"Bearer {settings.smtp_pass}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("text", ""), data.get("html", "")
    except Exception as exc:
        logger.warning("fetch_email_body_failed", email_id=email_id, error=str(exc))
        return "", ""


@router.post("/inbound/email")
async def inbound_email(request: Request) -> dict:
    """Receive inbound emails via Resend webhook.

    Resend sends an email.received event when someone emails agent@astrlboy.xyz.
    We fetch the full body, match against job applications, and notify Wave.
    """
    body = await request.body()

    # Signature verification — skip if no secret configured (dev mode)
    if settings.resend_webhook_secret:
        sig = request.headers.get("svix-signature")
        if not _verify_resend_signature(body, sig, settings.resend_webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()

    # Resend webhook: { type: "email.received", data: { email_id, from, to, subject, ... } }
    event_type = payload.get("type", "")
    if event_type != "email.received":
        return {"status": "ignored", "reason": f"unhandled event type: {event_type}"}

    data = payload.get("data", {})
    email_id = data.get("email_id", "")
    from_email = data.get("from", "")
    to_list = data.get("to", [])
    subject = data.get("subject", "")

    logger.info(
        "inbound_email_received",
        email_id=email_id,
        from_email=from_email,
        to=to_list,
        subject=subject,
    )

    # Fetch the actual email body from Resend API (webhook only has metadata)
    text_body, html_body = await _fetch_email_body(email_id) if email_id else ("", "")

    # Persist the email so the agent can search through it later
    matched_application = None
    stored_email_id = None
    try:
        async with async_session_factory() as session:
            # Match against job applications first
            result = await session.execute(
                select(JobApplication)
                .where(JobApplication.email_sent_to == from_email)
                .order_by(JobApplication.sent_at.desc())
                .limit(1)
            )
            matched_application = result.scalar_one_or_none()

            if matched_application:
                if matched_application.status == "sent":
                    matched_application.status = "replied"
                    matched_application.last_updated = datetime.now(timezone.utc)
                    logger.info(
                        "job_application_reply_matched",
                        application_id=str(matched_application.id),
                        company=matched_application.company,
                        from_email=from_email,
                    )

            # Store the email in the DB
            to_addr = to_list[0] if to_list else "agent@astrlboy.xyz"
            inbound = InboundEmail(
                resend_email_id=email_id or None,
                from_email=from_email,
                to_email=to_addr,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                matched_application_id=matched_application.id if matched_application else None,
            )
            session.add(inbound)
            await session.commit()
            stored_email_id = str(inbound.id)

            logger.info("inbound_email_stored", email_db_id=stored_email_id)
    except Exception as exc:
        logger.error("inbound_email_store_failed", error=str(exc))

    # Notify Wave via Telegram
    try:
        from telegram import Bot

        bot = Bot(token=settings.telegram_bot_token)

        # Show body preview — prefer plain text, fall back to noting HTML-only
        body_preview = text_body[:500] if text_body else "(HTML-only email — check Resend dashboard)"

        if matched_application:
            text = (
                f"Reply received — job application\n\n"
                f"From: {from_email}\n"
                f"Company: {matched_application.company}\n"
                f"Role: {matched_application.role}\n"
                f"Subject: {subject}\n\n"
                f"{body_preview}"
            )
        else:
            text = (
                f"New inbound email\n\n"
                f"From: {from_email}\n"
                f"Subject: {subject}\n\n"
                f"{body_preview}"
            )

        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
        )
    except Exception as exc:
        logger.warning("inbound_email_telegram_failed", error=str(exc))

    return {"status": "processed", "matched_application": matched_application is not None}
