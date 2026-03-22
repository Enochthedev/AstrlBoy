"""
X filtered stream subscription skill.

Manages stream rules for X API v2 filtered stream. The actual persistent
stream connection is in streams/x_stream.py — this skill handles
adding/removing filter rules.
"""

from typing import Any

import tweepy

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.trend_stream")


class TrendStreamSkill(BaseTool):
    """Manage X filtered stream rules for trend monitoring."""

    name = "trend_stream"
    description = "Add or remove X filtered stream rules. Manages what keywords the stream monitors."
    version = "1.0.0"

    def __init__(self) -> None:
        self._client = tweepy.Client(bearer_token=settings.twitter_bearer_token)

    async def execute(
        self,
        action: str = "list",
        keywords: list[str] | None = None,
        tag: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Manage stream filter rules.

        Args:
            action: 'add', 'remove', or 'list'.
            keywords: Keywords for the rule (required for 'add').
            tag: Optional tag for the rule.

        Returns:
            Dict with current rules or action result.

        Raises:
            SkillExecutionError: If rule management fails.
        """
        try:
            if action == "list":
                rules = self._client.get_rules()
                return {
                    "rules": [
                        {"id": r.id, "value": r.value, "tag": r.tag}
                        for r in (rules.data or [])
                    ]
                }

            if action == "add" and keywords:
                rule_value = " OR ".join(keywords)
                result = self._client.add_rules(
                    tweepy.StreamRule(value=rule_value, tag=tag or None)
                )
                logger.info("stream_rule_added", rule=rule_value, tag=tag)
                return {"added": rule_value, "tag": tag}

            if action == "remove" and keywords:
                rules = self._client.get_rules()
                ids_to_remove = [
                    r.id for r in (rules.data or [])
                    if any(k in r.value for k in keywords)
                ]
                if ids_to_remove:
                    self._client.delete_rules(ids_to_remove)
                    logger.info("stream_rules_removed", count=len(ids_to_remove))
                return {"removed_count": len(ids_to_remove)}

            return {"error": "Invalid action or missing keywords"}
        except Exception as exc:
            logger.error("trend_stream_failed", action=action, error=str(exc))
            raise SkillExecutionError(f"Stream rule management failed: {exc}") from exc

    def get_schema(self) -> dict:
        """Return JSON schema for trend_stream inputs."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "list"],
                    "default": "list",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keywords for the stream rule",
                },
                "tag": {"type": "string", "description": "Optional tag for the rule"},
            },
            "required": ["action"],
        }
