"""
Job applications LangGraph graph.

Scans boards, scores fit, drafts and sends applications.
"""

from typing import Any

from langgraph.graph import END, StateGraph

from core.logging import get_logger
from db.models.contracts import Contract
from graphs.base import BaseGraph
from graphs.applications.nodes import draft_application, scan_job_boards, score_fit
from graphs.applications.state import ApplicationState

logger = get_logger("graphs.applications")


class ApplicationsGraph(BaseGraph):
    """Job application pipeline."""

    def build(self) -> StateGraph:
        """Build the applications graph."""
        graph = StateGraph(ApplicationState)

        graph.add_node("scan_job_boards", scan_job_boards)
        graph.add_node("score_fit", score_fit)
        graph.add_node("draft_application", draft_application)

        graph.set_entry_point("scan_job_boards")
        graph.add_edge("scan_job_boards", "score_fit")
        graph.add_edge("score_fit", "draft_application")
        graph.add_edge("draft_application", END)

        return graph.compile()

    async def run(self, contract: Contract | None = None, **kwargs: Any) -> dict:
        """Run the applications graph. This is agent-level, not per-contract."""
        compiled = self.build()
        initial_state: ApplicationState = {}

        logger.info("applications_graph_started")
        result = await compiled.ainvoke(initial_state)
        logger.info("applications_graph_completed", sent=result.get("sent_count", 0))
        return result


# Singleton
applications_graph = ApplicationsGraph()
