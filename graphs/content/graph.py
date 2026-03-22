"""
Content generation LangGraph graph.

Wires up the content pipeline: research → draft → critique → revise (loop) → save → publish.
The critique loop runs up to 2 times before escalating to Wave.
"""

from typing import Any
from uuid import uuid4

from langgraph.graph import END, StateGraph

from core.logging import get_logger
from db.models.contracts import Contract
from graphs.base import BaseGraph
from graphs.content.nodes import (
    approve_or_escalate,
    generate_draft,
    publish,
    research_trends,
    revise,
    save,
    self_critique,
)
from graphs.content.state import ContentState

logger = get_logger("graphs.content")


def _should_revise_or_save(state: ContentState) -> str:
    """Route after self-critique: save if approved, revise if not, escalate if too many revisions."""
    if state.get("is_approved"):
        return "save"
    if state.get("revision_count", 0) >= 2:
        return "approve_or_escalate"
    return "revise"


class ContentGraph(BaseGraph):
    """Content generation pipeline with self-critique loop.

    Flow:
        research_trends → generate_draft → self_critique
        → (approved) → save → publish
        → (not approved, revisions < 2) → revise → self_critique
        → (not approved, revisions >= 2) → approve_or_escalate
    """

    def build(self) -> StateGraph:
        """Build the content generation graph.

        Returns:
            A compiled StateGraph.
        """
        graph = StateGraph(ContentState)

        graph.add_node("research_trends", research_trends)
        graph.add_node("generate_draft", generate_draft)
        graph.add_node("self_critique", self_critique)
        graph.add_node("revise", revise)
        graph.add_node("approve_or_escalate", approve_or_escalate)
        graph.add_node("save", save)
        graph.add_node("publish", publish)

        graph.set_entry_point("research_trends")
        graph.add_edge("research_trends", "generate_draft")
        graph.add_edge("generate_draft", "self_critique")

        graph.add_conditional_edges(
            "self_critique",
            _should_revise_or_save,
            {
                "save": "save",
                "revise": "revise",
                "approve_or_escalate": "approve_or_escalate",
            },
        )

        graph.add_edge("revise", "self_critique")
        graph.add_edge("approve_or_escalate", END)
        graph.add_edge("save", "publish")
        graph.add_edge("publish", END)

        return graph.compile()

    async def run(self, contract: Contract, content_type: str = "post", **kwargs: Any) -> dict:
        """Execute the content generation graph for a contract.

        Args:
            contract: The contract to generate content for.
            content_type: Type of content to generate.

        Returns:
            The final state after graph execution.
        """
        compiled = self.build()

        initial_state: ContentState = {
            "contract_id": contract.id,
            "contract_slug": contract.client_slug,
            "contract_meta": contract.meta,
            "content_type": content_type,
        }

        logger.info(
            "content_graph_started",
            contract_slug=contract.client_slug,
            content_type=content_type,
        )

        result = await compiled.ainvoke(initial_state)

        logger.info(
            "content_graph_completed",
            contract_slug=contract.client_slug,
            status=result.get("status", "unknown"),
        )

        return result


# Singleton
content_graph = ContentGraph()
