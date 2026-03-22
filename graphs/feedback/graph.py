"""
Product feedback / feature request LangGraph graph.

Gathers observations, generates structured feature requests, and submits them.
"""

from typing import Any

from langgraph.graph import END, StateGraph

from core.logging import get_logger
from db.models.contracts import Contract
from graphs.base import BaseGraph
from graphs.feedback.nodes import gather_observations, generate_feature_requests, submit_requests
from graphs.feedback.state import FeedbackState

logger = get_logger("graphs.feedback")


class FeedbackGraph(BaseGraph):
    """Product feedback pipeline."""

    def build(self) -> StateGraph:
        """Build the feedback graph."""
        graph = StateGraph(FeedbackState)

        graph.add_node("gather_observations", gather_observations)
        graph.add_node("generate_feature_requests", generate_feature_requests)
        graph.add_node("submit_requests", submit_requests)

        graph.set_entry_point("gather_observations")
        graph.add_edge("gather_observations", "generate_feature_requests")
        graph.add_edge("generate_feature_requests", "submit_requests")
        graph.add_edge("submit_requests", END)

        return graph.compile()

    async def run(self, contract: Contract, **kwargs: Any) -> dict:
        """Run the feedback graph for a contract."""
        compiled = self.build()
        initial_state: FeedbackState = {
            "contract_id": contract.id,
            "contract_slug": contract.client_slug,
            "contract_meta": contract.meta,
        }

        logger.info("feedback_graph_started", contract_slug=contract.client_slug)
        result = await compiled.ainvoke(initial_state)
        logger.info("feedback_graph_completed", contract_slug=contract.client_slug)
        return result


# Singleton
feedback_graph = FeedbackGraph()
