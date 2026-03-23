"""
Outbound email model — tracks every email the agent sends.

Every email sent via the send_email skill is persisted here so the agent
can see its own sent mail, build conversation threads with inbound replies,
and avoid duplicate sends.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class OutboundEmail(Base):
    """An email sent by astrlboy via the Resend API."""

    __tablename__ = "outbound_emails"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    resend_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    from_email: Mapped[str] = mapped_column(
        String(500), nullable=False, default="agent@astrlboy.xyz"
    )
    to_email: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    text_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    html_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # What triggered this email: 'application', 'briefing', 'follow_up', 'general'
    email_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="general"
    )
    # Optional link to the contract this email is for
    contract_slug: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
