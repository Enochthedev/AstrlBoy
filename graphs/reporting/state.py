"""
State definition for the weekly briefing/reporting graph.
"""

from datetime import date
from typing import TypedDict
from uuid import UUID


class ReportingState(TypedDict, total=False):
    """State flowing through the reporting graph."""

    contract_id: UUID
    contract_slug: str
    contract_meta: dict
    week_of: date

    # Aggregated data
    competitor_moves: str
    trend_signals: str
    opportunities: str
    content_ideas: str

    # Output
    briefing: str
    briefing_id: UUID
    status: str
    error: str | None
