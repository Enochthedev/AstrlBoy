"""
Job application model — tracks outbound applications from agent@astrlboy.xyz.

astrlboy scans job boards, scores fit, drafts applications,
and tracks the entire pipeline from sent to closed.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class JobApplication(Base):
    """A job application sent by astrlboy."""

    __tablename__ = "job_applications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    role: Mapped[str] = mapped_column(String(500), nullable=False)
    company: Mapped[str] = mapped_column(String(255), nullable=False)
    posting_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_sent_to: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cover_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    r2_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="sent")
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
