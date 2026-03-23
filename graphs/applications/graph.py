"""
Job applications LangGraph graph.

Scans boards, scores fit, warms up targets via X outreach,
then drafts and sends applications.
"""

from typing import Any

from langgraph.graph import END, StateGraph

from core.logging import get_logger
from db.models.contracts import Contract
from graphs.base import BaseGraph
from graphs.applications.nodes import (
    draft_application,
    scan_job_boards,
    score_fit,
    warm_outreach,
)
from graphs.applications.state import ApplicationState

logger = get_logger("graphs.applications")


class ApplicationsGraph(BaseGraph):
    """Job application pipeline with warm outreach before cold application."""

    async def build(self) -> Any:
        """Build the applications graph with checkpointer.

        Flow: scan → score → warm outreach → apply
        The warm outreach step engages with the hiring company on X
        before the application lands, creating recognition.
        """
        graph = StateGraph(ApplicationState)

        graph.add_node("scan_job_boards", scan_job_boards)
        graph.add_node("score_fit", score_fit)
        graph.add_node("warm_outreach", warm_outreach)
        graph.add_node("draft_application", draft_application)

        graph.set_entry_point("scan_job_boards")
        graph.add_edge("scan_job_boards", "score_fit")
        graph.add_edge("score_fit", "warm_outreach")
        graph.add_edge("warm_outreach", "draft_application")
        graph.add_edge("draft_application", END)

        try:
            from db.checkpointer import get_checkpointer
            checkpointer = await get_checkpointer()
            return graph.compile(checkpointer=checkpointer)
        except Exception as exc:
            logger.warning("checkpointer_unavailable", error=str(exc))
            return graph.compile()

    async def run(self, contract: Contract | None = None, **kwargs: Any) -> dict:
        """Run the applications graph. This is agent-level, not per-contract."""
        compiled = await self.build()
        initial_state: ApplicationState = {}

        logger.info("applications_graph_started")
        result = await compiled.ainvoke(initial_state)
        logger.info("applications_graph_completed", sent=result.get("sent_count", 0))
        return result


# Singleton
applications_graph = ApplicationsGraph()
