"""
Interaction model — community engagement tracking.

Every reply or thread engagement is logged here. Reddit and Discord
interactions require operator approval before posting.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Interaction(Base):
    """A community interaction (reply, comment, thread engagement)."""

    __tablename__ = "interactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contracts.id"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    thread_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    thread_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    r2_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
