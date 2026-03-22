"""
State definition for the intelligence (competitor + trend monitoring) graph.
"""

from typing import TypedDict
from uuid import UUID


class IntelligenceState(TypedDict, total=False):
    """State flowing through the intelligence monitoring graph."""

    contract_id: UUID
    contract_slug: str
    contract_meta: dict

    # Competitor analysis
    competitor_snapshots: list[dict]
    diff_from_last_week: str

    # Trend signals
    trend_signals: list[dict]
    scored_signals: list[dict]

    # Output
    opportunities: str
    status: str
    error: str | None
