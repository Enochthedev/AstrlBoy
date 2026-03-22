"""
State definition for the growth experiments graph.
"""

from typing import TypedDict
from uuid import UUID


class ExperimentState(TypedDict, total=False):
    """State flowing through the experiments graph."""

    contract_id: UUID
    contract_slug: str
    contract_meta: dict

    # Experiment lifecycle
    experiment_ideas: list[dict]
    selected_experiment: dict
    hypothesis: str
    execution_plan: str
    result: str
    learning: str

    # Output
    experiment_id: UUID
    status: str
    error: str | None
