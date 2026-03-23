"""
Long-term memory for astrlboy using mem0.

Stores facts, patterns, and learnings that persist across all sessions.
Call add() after significant events (content published, engagement patterns,
competitor changes, experiment results).
Call search() before tasks to retrieve relevant context.

Uses the mem0 platform API (AsyncMemoryClient) which handles extraction,
deduplication, and semantic retrieval automatically.
"""

from mem0 import AsyncMemoryClient

from core.config import settings
from core.logging import get_logger

logger = get_logger("memory.mem0")


class AgentMemory:
    """Long-term semantic memory for astrlboy.

    Wraps mem0's AsyncMemoryClient to store and retrieve memories
    scoped by contract (via user_id) and categorized by type (via metadata).

    Memories are automatically embedded and deduplicated by mem0.
    Retrieval is semantic — similar concepts match even with different wording.
    """

    def __init__(self) -> None:
        self._client: AsyncMemoryClient | None = None
        self._agent_id = "astrlboy"

    def _ensure_client(self) -> AsyncMemoryClient:
        """Lazily initialize the mem0 client.

        Raises:
            RuntimeError: If MEM0_API_KEY is not configured.
        """
        if self._client is None:
            if not settings.mem0_api_key:
                raise RuntimeError("MEM0_API_KEY not configured — cannot use long-term memory")
            self._client = AsyncMemoryClient(api_key=settings.mem0_api_key)
        return self._client

    @property
    def available(self) -> bool:
        """Whether mem0 is configured and available."""
        return bool(settings.mem0_api_key)

    async def add(
        self,
        content: str,
        contract_slug: str | None = None,
        category: str | None = None,
    ) -> None:
        """Store a new memory.

        mem0 handles extraction and deduplication — if a similar fact
        already exists, it updates rather than duplicating.

        Args:
            content: The fact or learning to remember.
            contract_slug: Which client this memory belongs to (optional).
            category: Type of memory e.g. 'engagement', 'content', 'competitor'.
        """
        client = self._ensure_client()

        metadata = {}
        if category:
            metadata["category"] = category

        # Use contract_slug as user_id for per-client scoping
        await client.add(
            messages=[{"role": "assistant", "content": content}],
            agent_id=self._agent_id,
            user_id=contract_slug or "global",
            metadata=metadata if metadata else None,
        )
        logger.info("memory_added", category=category, contract=contract_slug)

    async def search(
        self,
        query: str,
        contract_slug: str | None = None,
        limit: int = 5,
    ) -> list[str]:
        """Retrieve memories relevant to a query via semantic search.

        Args:
            query: What you're looking for e.g. "engagement patterns on X".
            contract_slug: Filter to a specific client's memories.
            limit: Max memories to return.

        Returns:
            List of relevant memory strings.
        """
        client = self._ensure_client()

        kwargs: dict = {
            "query": query,
            "agent_id": self._agent_id,
            "limit": limit,
        }
        if contract_slug:
            kwargs["user_id"] = contract_slug

        results = await client.search(**kwargs)

        # mem0 returns list of dicts with "memory" key
        memories = []
        if isinstance(results, dict) and "results" in results:
            memories = [r["memory"] for r in results["results"] if "memory" in r]
        elif isinstance(results, list):
            memories = [r["memory"] for r in results if isinstance(r, dict) and "memory" in r]

        return memories

    async def get_all(self, contract_slug: str | None = None) -> list[str]:
        """Get all memories, optionally filtered by contract.

        Args:
            contract_slug: Filter to a specific client's memories.

        Returns:
            List of all memory strings matching the filter.
        """
        client = self._ensure_client()

        kwargs: dict = {"agent_id": self._agent_id}
        if contract_slug:
            kwargs["user_id"] = contract_slug

        results = await client.get_all(**kwargs)

        memories = []
        if isinstance(results, dict) and "results" in results:
            memories = [r["memory"] for r in results["results"] if "memory" in r]
        elif isinstance(results, list):
            memories = [r["memory"] for r in results if isinstance(r, dict) and "memory" in r]

        return memories


# Singleton — import this everywhere
agent_memory = AgentMemory()
