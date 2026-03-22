"""
Briefing model — weekly intelligence briefings.

Delivered every Monday with competitor moves, trend signals,
and actionable opportunities synthesized by Claude.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Briefing(Base):
    """A weekly intelligence briefing for a client contract."""

    __tablename__ = "briefings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contracts.id"), nullable=False
    )
    week_of: Mapped[date] = mapped_column(Date, nullable=False)
    competitor_moves: Mapped[str | None] = mapped_column(Text, nullable=True)
    trend_signals: Mapped[str | None] = mapped_column(Text, nullable=True)
    opportunities: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_ideas: Mapped[str | None] = mapped_column(Text, nullable=True)
    r2_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
