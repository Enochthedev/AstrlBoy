"""
State definition for the content generation graph.

TypedDict that flows through all nodes — research, draft, critique, revise, publish.
"""

from typing import TypedDict
from uuid import UUID


class ContentState(TypedDict, total=False):
    """State flowing through the content generation graph."""

    # Input
    contract_id: UUID
    contract_slug: str
    contract_meta: dict
    content_type: str

    # Research phase
    research: str
    trend_signals: list[dict]

    # Generation phase
    draft: str
    title: str

    # Critique loop
    critique_notes: str
    is_approved: bool
    revision_count: int

    # Output
    content_id: UUID
    r2_key: str
    status: str
    error: str | None
