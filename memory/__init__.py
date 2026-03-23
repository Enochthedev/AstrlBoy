"""
Memory system for astrlboy — three layers of context and recall.

Layer 1: Working Memory — LangGraph checkpointer (graph state persistence)
Layer 2: Short-Term Memory — structured DB queries injected before graph runs
Layer 3: Long-Term Memory — mem0 semantic memory (facts, patterns, learnings)
"""
