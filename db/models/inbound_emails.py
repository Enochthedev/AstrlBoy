"""
Inbound email model — stores emails received via Resend webhook.

Every email sent to agent@astrlboy.xyz is persisted here so the agent
can search through past emails at any time without relying on IMAP.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class InboundEmail(Base):
    """An email received at agent@astrlboy.xyz via Resend webhook."""

    __tablename__ = "inbound_emails"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    resend_email_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    from_email: Mapped[str] = mapped_column(String(500), nullable=False)
    to_email: Mapped[str] = mapped_column(String(500), nullable=False, default="agent@astrlboy.xyz")
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    text_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    html_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    matched_application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
