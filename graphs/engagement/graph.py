"""
Community engagement LangGraph graph.

Finds threads, scores, filters, drafts replies, routes for approval, posts, and logs.
"""

from typing import Any

from langgraph.graph import END, StateGraph

from core.logging import get_logger
from db.models.contracts import Contract
from graphs.base import BaseGraph
from graphs.engagement.nodes import (
    draft_replies,
    filter_threads,
    find_threads,
    log_interactions,
    post,
    route_approval,
    score_threads,
)
from graphs.engagement.state import EngagementState

logger = get_logger("graphs.engagement")


class EngagementGraph(BaseGraph):
    """Community engagement pipeline with approval routing."""

    def build(self) -> StateGraph:
        """Build the engagement graph."""
        graph = StateGraph(EngagementState)

        graph.add_node("find_threads", find_threads)
        graph.add_node("score_threads", score_threads)
        graph.add_node("filter_threads", filter_threads)
        graph.add_node("draft_replies", draft_replies)
        graph.add_node("route_approval", route_approval)
        graph.add_node("post", post)
        graph.add_node("log_interactions", log_interactions)

        graph.set_entry_point("find_threads")
        graph.add_edge("find_threads", "score_threads")
        graph.add_edge("score_threads", "filter_threads")
        graph.add_edge("filter_threads", "draft_replies")
        graph.add_edge("draft_replies", "route_approval")
        graph.add_edge("route_approval", "post")
        graph.add_edge("post", "log_interactions")
        graph.add_edge("log_interactions", END)

        return graph.compile()

    async def run(self, contract: Contract, platform: str = "x", **kwargs: Any) -> dict:
        """Run the engagement graph for a contract on a specific platform."""
        compiled = self.build()
        initial_state: EngagementState = {
            "contract_id": contract.id,
            "contract_slug": contract.client_slug,
            "contract_meta": contract.meta,
            "platform": platform,
        }

        logger.info("engagement_graph_started", contract_slug=contract.client_slug, platform=platform)
        result = await compiled.ainvoke(initial_state)
        logger.info("engagement_graph_completed", contract_slug=contract.client_slug)
        return result


# Singleton
engagement_graph = EngagementGraph()
