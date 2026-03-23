"""
State definition for the community engagement graph.
"""

from typing import TypedDict
from uuid import UUID


class EngagementState(TypedDict, total=False):
    """State flowing through the community engagement graph."""

    contract_id: UUID
    contract_slug: str
    contract_meta: dict
    platform: str

    # Memory context — injected before first node runs
    context: dict  # from build_engagement_context
    long_term_memories: list[str]  # from mem0 search

    # Thread discovery
    candidate_threads: list[dict]
    scored_threads: list[dict]
    filtered_threads: list[dict]

    # Draft generation
    drafts: list[dict]
    approved_drafts: list[dict]

    # Output
    posted_count: int
    status: str
    error: str | None
