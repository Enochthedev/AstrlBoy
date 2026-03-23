"""
Base class for all LangGraph graphs.

Each responsibility (content, intelligence, engagement, etc.) is its own graph.
All graphs implement this interface so the scheduler can run them uniformly.
"""

from abc import ABC, abstractmethod
from typing import Any

from langgraph.graph import StateGraph

from db.models.contracts import Contract


class BaseGraph(ABC):
    """Abstract base for all astrlboy LangGraph graphs.

    To add a new graph:
    1. Create a folder in graphs/
    2. Define state.py (TypedDict)
    3. Define nodes.py (one async function per node)
    4. Define graph.py (wire nodes + edges)
    5. Register in scheduler/jobs.py
    Nothing else needs to change.
    """

    @abstractmethod
    async def build(self) -> Any:
        """Build and return the compiled graph.

        Async to support checkpointer initialization.

        Returns:
            A compiled LangGraph StateGraph ready to invoke.
        """
        pass

    @abstractmethod
    async def run(self, contract: Contract, **kwargs: Any) -> dict:
        """Execute the graph for a given contract.

        Args:
            contract: The contract to run the graph for.
            **kwargs: Additional parameters specific to the graph.

        Returns:
            The final state dict after graph execution.
        """
        pass
