"""
Content generation LangGraph graph.

Wires up the content pipeline: research → draft → critique → revise (loop) → save → publish.
The critique loop runs up to 2 times before escalating to Wave.

Memory integration:
- Layer 1: Checkpointer enables resume-on-failure
- Layer 2: build_content_context injects recent content/trends/experiments
- Layer 3: mem0 search retrieves long-term content strategy learnings
"""

from datetime import date
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

    async def build(self) -> Any:
        """Build the content generation graph with checkpointer.

        Returns:
            A compiled StateGraph with checkpoint persistence.
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

        # Layer 1: checkpointer for resume-on-failure
        try:
            from db.checkpointer import get_checkpointer
            checkpointer = await get_checkpointer()
            return graph.compile(checkpointer=checkpointer)
        except Exception as exc:
            logger.warning("checkpointer_unavailable", error=str(exc))
            return graph.compile()

    async def run(self, contract: Contract, content_type: str = "post", **kwargs: Any) -> dict:
        """Execute the content generation graph for a contract.

        Injects Layer 2 (DB context) and Layer 3 (mem0 memories) before
        running the graph. Stores new memories after completion.

        Args:
            contract: The contract to generate content for.
            content_type: Type of content to generate.

        Returns:
            The final state after graph execution.
        """
        compiled = await self.build()

        # Layer 2: inject structured DB context
        context = {}
        try:
            from memory.context_builder import build_content_context
            context = await build_content_context(contract.id, content_type)
        except Exception as exc:
            logger.warning("content_context_build_failed", error=str(exc))

        # Layer 3: retrieve relevant long-term memories
        long_term_memories: list[str] = []
        try:
            from memory.mem0_client import agent_memory
            if agent_memory.available:
                long_term_memories = await agent_memory.search(
                    query=f"content strategy for {contract.client_name} {content_type}",
                    contract_slug=contract.client_slug,
                    limit=5,
                )
        except Exception as exc:
            logger.warning("mem0_search_failed", error=str(exc))

        initial_state: ContentState = {
            "contract_id": contract.id,
            "contract_slug": contract.client_slug,
            "contract_meta": contract.meta,
            "content_type": content_type,
            "context": context,
            "long_term_memories": long_term_memories,
        }

        # Thread ID for checkpointer — unique per contract + date + hour bucket
        # Hour bucket (am/pm) ensures morning and afternoon runs don't share state
        from datetime import datetime
        now = datetime.now()
        slot = "am" if now.hour < 13 else "pm"
        today = date.today().isoformat()
        config = {"configurable": {"thread_id": f"{contract.client_slug}_content_{today}_{slot}"}}

        logger.info(
            "content_graph_started",
            contract_slug=contract.client_slug,
            content_type=content_type,
            context_items=len(context.get("recent_content", [])),
            memories=len(long_term_memories),
        )

        result = await compiled.ainvoke(initial_state, config=config)

        # Layer 3: store new memory after successful publish
        if result.get("status") in ("published", "pending_approval"):
            try:
                from memory.mem0_client import agent_memory
                if agent_memory.available:
                    await agent_memory.add(
                        content=(
                            f"Published {content_type} titled '{result.get('title', 'untitled')}'. "
                            f"Revisions: {result.get('revision_count', 0)}. "
                            f"Platform: {contract.meta.get('platforms', ['unknown'])[0]}."
                        ),
                        contract_slug=contract.client_slug,
                        category="content",
                    )
            except Exception as exc:
                logger.warning("mem0_store_failed", error=str(exc))

        logger.info(
            "content_graph_completed",
            contract_slug=contract.client_slug,
            status=result.get("status", "unknown"),
        )

        return result


# Singleton
content_graph = ContentGraph()
