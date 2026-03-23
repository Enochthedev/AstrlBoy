"""
State definition for the job applications graph.
"""

from typing import TypedDict
from uuid import UUID


class ApplicationState(TypedDict, total=False):
    """State flowing through the job applications graph."""

    # Discovery
    postings: list[dict]
    scored_postings: list[dict]
    selected: list[dict]

    # Warm outreach — engagement before cold application
    outreach_results: list[dict]

    # Application
    draft_application: str
    cover_note: str

    # Output
    sent_count: int
    status: str
    error: str | None
