"""
Central registry for all skills.

Graphs request skills by name — the registry resolves and returns them.
Skills can be enabled/disabled per contract via meta.active_skills.
"""

from core.exceptions import SkillNotFound
from core.logging import get_logger
from contracts.service import contracts_service
from skills.base import BaseTool

logger = get_logger("skills.registry")


class SkillRegistry:
    """Central registry mapping skill names to BaseTool instances.

    Skills are registered at startup and looked up by graphs at runtime.
    Per-contract filtering uses the active_skills list in contract meta.
    """

    def __init__(self) -> None:
        self._skills: dict[str, BaseTool] = {}

    async def register(self, skill: BaseTool) -> None:
        """Register a skill instance.

        Args:
            skill: The BaseTool instance to register.
        """
        self._skills[skill.name] = skill
        logger.info("skill_registered", name=skill.name, version=skill.version)

    async def get(self, name: str) -> BaseTool:
        """Get a skill by name.

        Args:
            name: The skill's unique identifier.

        Returns:
            The matching BaseTool instance.

        Raises:
            SkillNotFound: If no skill is registered with that name.
        """
        skill = self._skills.get(name)
        if skill is None:
            raise SkillNotFound(f"Skill '{name}' not registered")
        return skill

    async def list_all(self) -> list[BaseTool]:
        """Return all registered skills.

        Returns:
            List of all BaseTool instances.
        """
        return list(self._skills.values())

    async def list_for_contract(self, contract_slug: str) -> list[BaseTool]:
        """Return skills enabled for a specific contract.

        Filters the registry against the contract's meta.active_skills list.

        Args:
            contract_slug: The client slug.

        Returns:
            List of BaseTool instances enabled for this contract.
        """
        active_names = await contracts_service.get_active_skills(contract_slug)
        return [s for name, s in self._skills.items() if name in active_names]

    async def is_available(self, name: str) -> bool:
        """Check if a skill is registered.

        Args:
            name: The skill name.

        Returns:
            True if the skill exists in the registry.
        """
        return name in self._skills


# Singleton
skill_registry = SkillRegistry()
