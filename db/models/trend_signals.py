"""
Trend signal model — realtime and polled trend data.

Signals come from the X filtered stream, Reddit, Tavily, and Firecrawl.
Each is scored for relevance to the client contract.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TrendSignal(Base):
    """A trend signal captured from a monitoring source."""

    __tablename__ = "trend_signals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contracts.id"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    signal: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    r2_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
