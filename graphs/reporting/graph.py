"""
Weekly briefing LangGraph graph.

Aggregates intelligence, synthesizes, delivers via email, and stores.
"""

from datetime import date
from typing import Any

from langgraph.graph import END, StateGraph

from core.logging import get_logger
from db.models.contracts import Contract
from graphs.base import BaseGraph
from graphs.reporting.nodes import aggregate_intelligence, deliver, store, synthesize
from graphs.reporting.state import ReportingState

logger = get_logger("graphs.reporting")


class ReportingGraph(BaseGraph):
    """Weekly intelligence briefing pipeline."""

    async def build(self) -> Any:
        """Build the reporting graph with checkpointer."""
        graph = StateGraph(ReportingState)

        graph.add_node("aggregate_intelligence", aggregate_intelligence)
        graph.add_node("synthesize", synthesize)
        graph.add_node("deliver", deliver)
        graph.add_node("store", store)

        graph.set_entry_point("aggregate_intelligence")
        graph.add_edge("aggregate_intelligence", "synthesize")
        graph.add_edge("synthesize", "deliver")
        graph.add_edge("deliver", "store")
        graph.add_edge("store", END)

        try:
            from db.checkpointer import get_checkpointer
            checkpointer = await get_checkpointer()
            return graph.compile(checkpointer=checkpointer)
        except Exception as exc:
            logger.warning("checkpointer_unavailable", error=str(exc))
            return graph.compile()

    async def run(self, contract: Contract, **kwargs: Any) -> dict:
        """Run the reporting graph for a contract."""
        compiled = await self.build()
        initial_state: ReportingState = {
            "contract_id": contract.id,
            "contract_slug": contract.client_slug,
            "contract_meta": contract.meta,
            "week_of": date.today(),
        }

        logger.info("reporting_graph_started", contract_slug=contract.client_slug)
        result = await compiled.ainvoke(initial_state)
        logger.info("reporting_graph_completed", contract_slug=contract.client_slug)
        return result


# Singleton
reporting_graph = ReportingGraph()
