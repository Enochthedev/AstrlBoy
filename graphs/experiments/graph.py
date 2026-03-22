"""
Growth experiment lifecycle LangGraph graph.

Generates ideas, selects the best, and kicks off execution tracking.
"""

from typing import Any

from langgraph.graph import END, StateGraph

from core.logging import get_logger
from db.models.contracts import Contract
from graphs.base import BaseGraph
from graphs.experiments.nodes import generate_ideas, save_experiment, select_experiment
from graphs.experiments.state import ExperimentState

logger = get_logger("graphs.experiments")


class ExperimentsGraph(BaseGraph):
    """Growth experiment lifecycle pipeline."""

    def build(self) -> StateGraph:
        """Build the experiments graph."""
        graph = StateGraph(ExperimentState)

        graph.add_node("generate_ideas", generate_ideas)
        graph.add_node("select_experiment", select_experiment)
        graph.add_node("save_experiment", save_experiment)

        graph.set_entry_point("generate_ideas")
        graph.add_edge("generate_ideas", "select_experiment")
        graph.add_edge("select_experiment", "save_experiment")
        graph.add_edge("save_experiment", END)

        return graph.compile()

    async def run(self, contract: Contract, **kwargs: Any) -> dict:
        """Run the experiments graph for a contract."""
        compiled = self.build()
        initial_state: ExperimentState = {
            "contract_id": contract.id,
            "contract_slug": contract.client_slug,
            "contract_meta": contract.meta,
        }

        logger.info("experiments_graph_started", contract_slug=contract.client_slug)
        result = await compiled.ainvoke(initial_state)
        logger.info("experiments_graph_completed", contract_slug=contract.client_slug)
        return result


# Singleton
experiments_graph = ExperimentsGraph()
