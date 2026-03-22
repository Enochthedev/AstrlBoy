"""
Intelligence (competitor + trend monitoring) LangGraph graph.

Linear pipeline with filtering at the score step.
"""

from typing import Any

from langgraph.graph import END, StateGraph

from core.logging import get_logger
from db.models.contracts import Contract
from graphs.base import BaseGraph
from graphs.intelligence.nodes import (
    diff_snapshots,
    identify_opportunities,
    score_signals,
    scrape_competitors,
    search_trends,
    store_signals,
)
from graphs.intelligence.state import IntelligenceState

logger = get_logger("graphs.intelligence")


class IntelligenceGraph(BaseGraph):
    """Competitor and trend monitoring pipeline."""

    def build(self) -> StateGraph:
        """Build the intelligence graph."""
        graph = StateGraph(IntelligenceState)

        graph.add_node("scrape_competitors", scrape_competitors)
        graph.add_node("diff_snapshots", diff_snapshots)
        graph.add_node("search_trends", search_trends)
        graph.add_node("score_signals", score_signals)
        graph.add_node("store_signals", store_signals)
        graph.add_node("identify_opportunities", identify_opportunities)

        graph.set_entry_point("scrape_competitors")
        graph.add_edge("scrape_competitors", "diff_snapshots")
        graph.add_edge("diff_snapshots", "search_trends")
        graph.add_edge("search_trends", "score_signals")
        graph.add_edge("score_signals", "store_signals")
        graph.add_edge("store_signals", "identify_opportunities")
        graph.add_edge("identify_opportunities", END)

        return graph.compile()

    async def run(self, contract: Contract, **kwargs: Any) -> dict:
        """Run the intelligence graph for a contract."""
        compiled = self.build()
        initial_state: IntelligenceState = {
            "contract_id": contract.id,
            "contract_slug": contract.client_slug,
            "contract_meta": contract.meta,
        }

        logger.info("intelligence_graph_started", contract_slug=contract.client_slug)
        result = await compiled.ainvoke(initial_state)
        logger.info("intelligence_graph_completed", contract_slug=contract.client_slug)
        return result


# Singleton
intelligence_graph = IntelligenceGraph()
