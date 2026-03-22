"""
Contract runtime registry.

Maps client_slug to loaded contract config at runtime.
Populated on startup and refreshed when contracts change.
"""

from uuid import UUID

from core.exceptions import ContractNotFound
from core.logging import get_logger
from contracts.schema import ContractMeta

logger = get_logger("contracts.registry")


class ContractEntry:
    """A loaded contract entry in the runtime registry."""

    def __init__(
        self,
        contract_id: UUID,
        client_name: str,
        client_slug: str,
        client_db_url: str,
        meta: ContractMeta,
    ) -> None:
        self.contract_id = contract_id
        self.client_name = client_name
        self.client_slug = client_slug
        self.client_db_url = client_db_url
        self.meta = meta


class ContractRegistry:
    """Maps client_slug to contract config at runtime.

    Graphs and skills look up client config here instead of
    hitting the database on every invocation.
    """

    def __init__(self) -> None:
        self._entries: dict[str, ContractEntry] = {}

    def register(self, entry: ContractEntry) -> None:
        """Register or update a contract entry.

        Args:
            entry: The contract entry to register.
        """
        self._entries[entry.client_slug] = entry
        logger.info("contract_registered", slug=entry.client_slug)

    def unregister(self, slug: str) -> None:
        """Remove a contract entry.

        Args:
            slug: The client slug to remove.
        """
        self._entries.pop(slug, None)
        logger.info("contract_unregistered", slug=slug)

    def get(self, slug: str) -> ContractEntry:
        """Get a contract entry by slug.

        Args:
            slug: The client slug.

        Returns:
            The matching ContractEntry.

        Raises:
            ContractNotFound: If the slug is not registered.
        """
        entry = self._entries.get(slug)
        if entry is None:
            raise ContractNotFound(f"Contract '{slug}' not found in registry")
        return entry

    def list_all(self) -> list[ContractEntry]:
        """Return all registered contract entries.

        Returns:
            List of all ContractEntry objects.
        """
        return list(self._entries.values())

    def list_slugs(self) -> list[str]:
        """Return all registered client slugs.

        Returns:
            List of slug strings.
        """
        return list(self._entries.keys())


# Singleton
contract_registry = ContractRegistry()
