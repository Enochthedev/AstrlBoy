"""
Long-term memory skill — persist facts and learnings across sessions.

Use this to save anything worth remembering beyond the current conversation:
engagement patterns, preferences, context about accounts, decisions made,
things Wave told you to keep in mind, etc.

mem0 handles deduplication and semantic retrieval — if a similar fact already
exists, it updates it rather than duplicating. Future sessions can then recall
these memories via the search/recall flow in the system prompt.

Use this proactively during tasks, not just when explicitly asked.
"""

from typing import Any

from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.remember")


class RememberSkill(BaseTool):
    """Save a fact or learning to long-term semantic memory.

    Backed by mem0 — semantically indexed, deduplicated, and searchable
    across all future sessions. Per-client memories are scoped by contract_slug.
    """

    name = "remember"
    description = (
        "Save a fact, pattern, or learning to long-term memory so it persists across sessions. "
        "Use for: engagement patterns ('threads perform 3x better than single tweets'), "
        "preferences ('Wave prefers concise replies under 150 chars'), "
        "account context ('@elonmusk is sensitive to AI agent discussions'), "
        "decisions made ('decided to focus on builders not investors for this contract'), "
        "anything Wave tells you to keep in mind. "
        "mem0 deduplicates automatically — safe to call even if similar info exists."
    )
    version = "1.0.0"

    async def execute(
        self,
        content: str,
        category: str = "general",
        contract_slug: str | None = None,
    ) -> dict[str, Any]:
        """Save content to long-term memory.

        Args:
            content: The fact, pattern, or learning to remember.
            category: Type label — engagement, content, competitor, preference, general.
            contract_slug: Client slug for per-contract scoping. Omit for global memories.

        Returns:
            Dict with status and preview of what was saved.
        """
        from memory.mem0_client import agent_memory

        if not agent_memory.available:
            return {
                "status": "skipped",
                "reason": "MEM0_API_KEY not configured — long-term memory unavailable",
            }

        await agent_memory.add(
            content=content,
            contract_slug=contract_slug,
            category=category,
        )

        logger.info(
            "memory_saved",
            category=category,
            contract=contract_slug,
            preview=content[:80],
        )

        return {
            "status": "remembered",
            "category": category,
            "contract": contract_slug or "global",
            "preview": content[:120],
        }

    def get_schema(self) -> dict:
        """JSON schema for the remember skill inputs."""
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact, pattern, or learning to save",
                },
                "category": {
                    "type": "string",
                    "description": "Type of memory: engagement, content, competitor, preference, general",
                    "enum": ["engagement", "content", "competitor", "preference", "general"],
                },
                "contract_slug": {
                    "type": "string",
                    "description": "Client slug for per-contract scoping (e.g. 'mentorable'). Omit for global.",
                },
            },
            "required": ["content"],
        }
