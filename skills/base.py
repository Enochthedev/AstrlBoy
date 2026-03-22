"""
Base class for all skills.

Every external capability astrlboy uses is a skill implementing this interface.
To add a new skill: create a file, implement BaseTool, register in SkillRegistry.
Nothing else needs to change.
"""

from abc import ABC, abstractmethod
from typing import Any


class BaseTool(ABC):
    """Abstract base for all astrlboy skills.

    Attributes:
        name: Unique identifier (e.g. 'scrape').
        description: What this skill does — used in LangGraph tool nodes.
        version: Semver string (e.g. '1.0.0').
    """

    name: str
    description: str
    version: str

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        """Execute the skill.

        Args:
            **kwargs: Skill-specific parameters.

        Returns:
            Skill-specific result.
        """
        pass

    @abstractmethod
    def get_schema(self) -> dict:
        """Return JSON schema for this skill's inputs.

        Used by LangGraph to validate tool calls.

        Returns:
            A JSON Schema dict describing accepted parameters.
        """
        pass

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name} v{self.version}>"
