"""
Community engagement LangGraph graph.

Finds threads, scores, filters, drafts replies, routes for approval, posts, and logs.

Memory integration:
- Layer 1: Checkpointer enables resume-on-failure
- Layer 2: build_engagement_context injects recent interactions
- Layer 3: mem0 search retrieves engagement pattern learnings
"""

from datetime import date
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

    async def build(self) -> Any:
        """Build the engagement graph with checkpointer."""
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

        try:
            from db.checkpointer import get_checkpointer
            checkpointer = await get_checkpointer()
            return graph.compile(checkpointer=checkpointer)
        except Exception as exc:
            logger.warning("checkpointer_unavailable", error=str(exc))
            return graph.compile()

    async def run(self, contract: Contract, platform: str = "x", **kwargs: Any) -> dict:
        """Run the engagement graph with memory context injection."""
        compiled = await self.build()

        # Layer 2: inject structured DB context
        context = {}
        try:
            from memory.context_builder import build_engagement_context
            context = await build_engagement_context(contract.id, platform)
        except Exception as exc:
            logger.warning("engagement_context_build_failed", error=str(exc))

        # Layer 3: retrieve relevant long-term memories
        long_term_memories: list[str] = []
        try:
            from memory.mem0_client import agent_memory
            if agent_memory.available:
                long_term_memories = await agent_memory.search(
                    query=f"engagement patterns on {platform} for {contract.client_name}",
                    contract_slug=contract.client_slug,
                    limit=5,
                )
        except Exception as exc:
            logger.warning("mem0_search_failed", error=str(exc))

        initial_state: EngagementState = {
            "contract_id": contract.id,
            "contract_slug": contract.client_slug,
            "contract_meta": contract.meta,
            "platform": platform,
            "context": context,
            "long_term_memories": long_term_memories,
        }

        today = date.today().isoformat()
        config = {"configurable": {"thread_id": f"{contract.client_slug}_engagement_{platform}_{today}"}}

        logger.info(
            "engagement_graph_started",
            contract_slug=contract.client_slug,
            platform=platform,
            context_items=len(context.get("engaged_this_week", [])),
            memories=len(long_term_memories),
        )

        result = await compiled.ainvoke(initial_state, config=config)

        # Layer 3: store engagement outcome
        posted = result.get("posted_count", 0)
        if posted > 0:
            try:
                from memory.mem0_client import agent_memory
                if agent_memory.available:
                    await agent_memory.add(
                        content=(
                            f"Engagement run on {platform}: posted {posted} replies. "
                            f"Threads found: {len(result.get('candidate_threads', []))}. "
                            f"Filtered to: {len(result.get('filtered_threads', []))}."
                        ),
                        contract_slug=contract.client_slug,
                        category="engagement",
                    )
            except Exception as exc:
                logger.warning("mem0_store_failed", error=str(exc))

        logger.info("engagement_graph_completed", contract_slug=contract.client_slug, posted=posted)
        return result


# Singleton
engagement_graph = EngagementGraph()
