"""
Contracts service — the main interface for contract CRUD.

All scheduled jobs and graphs call get_active_contracts() and iterate.
Never hardcode a client anywhere in the codebase.
"""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.constants import ContractStatus
from core.exceptions import ContractNotFound
from core.logging import get_logger
from contracts.registry import ContractEntry, contract_registry
from contracts.schema import ContractCreate, ContractMeta
from db.base import async_session_factory
from db.models.contracts import Contract

logger = get_logger("contracts.service")


class ContractsService:
    """Manages contract lifecycle — create, list, pause, complete.

    All methods use their own sessions. Callers do not need to
    manage database sessions.
    """

    async def get_active_contracts(self) -> list[Contract]:
        """Return all contracts with status 'active'.

        Returns:
            List of active Contract ORM objects.
        """
        async with async_session_factory() as session:
            result = await session.execute(
                select(Contract).where(Contract.status == ContractStatus.ACTIVE)
            )
            return list(result.scalars().all())

    async def get_contract(self, slug: str) -> Contract:
        """Return a single contract by slug.

        Args:
            slug: The client_slug to look up.

        Returns:
            The Contract ORM object.

        Raises:
            ContractNotFound: If no contract exists with that slug.
        """
        async with async_session_factory() as session:
            result = await session.execute(
                select(Contract).where(Contract.client_slug == slug)
            )
            contract = result.scalar_one_or_none()
            if contract is None:
                raise ContractNotFound(f"Contract '{slug}' not found")
            return contract

    async def get_meta(self, slug: str) -> dict:
        """Return the meta config dict for a contract.

        Args:
            slug: The client_slug.

        Returns:
            The meta JSONB dict.

        Raises:
            ContractNotFound: If no contract exists with that slug.
        """
        contract = await self.get_contract(slug)
        return contract.meta

    async def get_active_skills(self, slug: str) -> list[str]:
        """Return the list of active skill names for a contract.

        Args:
            slug: The client_slug.

        Returns:
            List of skill name strings.
        """
        meta = await self.get_meta(slug)
        return meta.get("active_skills", [])

    async def create_contract(self, data: ContractCreate) -> Contract:
        """Create a new contract and register it in the runtime registry.

        Args:
            data: The contract creation payload.

        Returns:
            The created Contract ORM object.
        """
        async with async_session_factory() as session:
            contract = Contract(
                client_name=data.client_name,
                client_slug=data.client_slug,
                client_db_url=data.client_db_url,
                meta=data.meta.model_dump(),
                ends_at=data.ends_at,
            )
            session.add(contract)
            await session.commit()
            await session.refresh(contract)

            # Register in runtime registry
            contract_registry.register(
                ContractEntry(
                    contract_id=contract.id,
                    client_name=contract.client_name,
                    client_slug=contract.client_slug,
                    client_db_url=contract.client_db_url,
                    meta=data.meta,
                )
            )

            logger.info("contract_created", slug=data.client_slug)
            return contract

    async def pause_contract(self, slug: str) -> Contract:
        """Pause an active contract.

        Args:
            slug: The client_slug.

        Returns:
            The updated Contract ORM object.

        Raises:
            ContractNotFound: If no contract exists with that slug.
        """
        async with async_session_factory() as session:
            result = await session.execute(
                select(Contract).where(Contract.client_slug == slug)
            )
            contract = result.scalar_one_or_none()
            if contract is None:
                raise ContractNotFound(f"Contract '{slug}' not found")

            contract.status = ContractStatus.PAUSED
            await session.commit()
            await session.refresh(contract)

            contract_registry.unregister(slug)
            logger.info("contract_paused", slug=slug)
            return contract

    async def complete_contract(self, slug: str) -> Contract:
        """Mark a contract as completed.

        Args:
            slug: The client_slug.

        Returns:
            The updated Contract ORM object.

        Raises:
            ContractNotFound: If no contract exists with that slug.
        """
        async with async_session_factory() as session:
            result = await session.execute(
                select(Contract).where(Contract.client_slug == slug)
            )
            contract = result.scalar_one_or_none()
            if contract is None:
                raise ContractNotFound(f"Contract '{slug}' not found")

            contract.status = ContractStatus.COMPLETED
            await session.commit()
            await session.refresh(contract)

            contract_registry.unregister(slug)
            logger.info("contract_completed", slug=slug)
            return contract

    async def load_registry(self) -> None:
        """Load all active contracts into the runtime registry.

        Called during application startup.
        """
        contracts = await self.get_active_contracts()
        for contract in contracts:
            meta = ContractMeta(**contract.meta)
            contract_registry.register(
                ContractEntry(
                    contract_id=contract.id,
                    client_name=contract.client_name,
                    client_slug=contract.client_slug,
                    client_db_url=contract.client_db_url,
                    meta=meta,
                )
            )
        logger.info("registry_loaded", count=len(contracts))


# Singleton
contracts_service = ContractsService()
