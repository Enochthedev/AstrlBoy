"""
Intelligence (competitor + trend monitoring) LangGraph graph.

Linear pipeline with filtering at the score step.

Memory integration:
- Layer 1: Checkpointer enables resume-on-failure
- Layer 2: build_intelligence_context injects previous briefings/signals
- Layer 3: mem0 search retrieves long-term competitor learnings
"""

from datetime import date
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

    async def build(self) -> Any:
        """Build the intelligence graph with checkpointer."""
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

        try:
            from db.checkpointer import get_checkpointer
            checkpointer = await get_checkpointer()
            return graph.compile(checkpointer=checkpointer)
        except Exception as exc:
            logger.warning("checkpointer_unavailable", error=str(exc))
            return graph.compile()

    async def run(self, contract: Contract, **kwargs: Any) -> dict:
        """Run the intelligence graph with memory context injection."""
        compiled = await self.build()

        # Layer 2: inject structured DB context
        context = {}
        try:
            from memory.context_builder import build_intelligence_context
            context = await build_intelligence_context(contract.id)
        except Exception as exc:
            logger.warning("intelligence_context_build_failed", error=str(exc))

        # Layer 3: retrieve relevant long-term memories
        long_term_memories: list[str] = []
        try:
            from memory.mem0_client import agent_memory
            if agent_memory.available:
                long_term_memories = await agent_memory.search(
                    query=f"competitor intelligence and market trends for {contract.client_name}",
                    contract_slug=contract.client_slug,
                    limit=5,
                )
        except Exception as exc:
            logger.warning("mem0_search_failed", error=str(exc))

        initial_state: IntelligenceState = {
            "contract_id": contract.id,
            "contract_slug": contract.client_slug,
            "contract_meta": contract.meta,
            "context": context,
            "long_term_memories": long_term_memories,
        }

        today = date.today().isoformat()
        config = {"configurable": {"thread_id": f"{contract.client_slug}_intelligence_{today}"}}

        logger.info(
            "intelligence_graph_started",
            contract_slug=contract.client_slug,
            context_signals=len(context.get("recent_signals", [])),
            memories=len(long_term_memories),
        )

        result = await compiled.ainvoke(initial_state, config=config)

        # Layer 3: store opportunities as long-term memory
        opportunities = result.get("opportunities", "")
        if opportunities:
            try:
                from memory.mem0_client import agent_memory
                if agent_memory.available:
                    await agent_memory.add(
                        content=f"Intelligence sweep: {opportunities[:500]}",
                        contract_slug=contract.client_slug,
                        category="competitor",
                    )
            except Exception as exc:
                logger.warning("mem0_store_failed", error=str(exc))

        logger.info("intelligence_graph_completed", contract_slug=contract.client_slug)
        return result


# Singleton
intelligence_graph = IntelligenceGraph()
