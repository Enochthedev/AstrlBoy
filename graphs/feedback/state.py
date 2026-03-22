"""
State definition for the product feedback / feature request graph.
"""

from typing import TypedDict
from uuid import UUID


class FeedbackState(TypedDict, total=False):
    """State flowing through the feedback graph."""

    contract_id: UUID
    contract_slug: str
    contract_meta: dict

    # Analysis
    observations: list[dict]
    feature_requests: list[dict]

    # Output
    submitted_count: int
    status: str
    error: str | None
