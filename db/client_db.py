"""
Per-client database connection manager.

Each contract gets its own Neon PostgreSQL instance. This module
manages a pool of engines keyed by contract ID, with lazy init
and graceful shutdown.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.logging import get_logger
from db.base import _normalize_db_url

logger = get_logger("db.client_db")


class ClientDBManager:
    """Manages async engines and sessions for per-client databases.

    Connection pools are created lazily on first access and cached
    for the lifetime of the application.
    """

    def __init__(self) -> None:
        self._engines: dict[UUID, AsyncEngine] = {}
        self._session_factories: dict[UUID, async_sessionmaker[AsyncSession]] = {}

    def _create_engine(self, contract_id: UUID, db_url: str) -> AsyncEngine:
        """Create and cache an async engine for a client database.

        Args:
            contract_id: The contract's UUID, used as the cache key.
            db_url: The client's database connection string.

        Returns:
            An AsyncEngine for the client's database.
        """
        eng = create_async_engine(_normalize_db_url(db_url), echo=False, pool_size=5, max_overflow=10)
        self._engines[contract_id] = eng
        self._session_factories[contract_id] = async_sessionmaker(
            eng,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        logger.info("client_db_connected", contract_id=str(contract_id))
        return eng

    async def get_session(self, contract_id: UUID, db_url: str) -> AsyncSession:
        """Return an async session for the given client's database.

        Lazily creates the engine on first access.

        Args:
            contract_id: The contract's UUID.
            db_url: The client's database connection string.

        Returns:
            An AsyncSession for the client's database.
        """
        if contract_id not in self._session_factories:
            self._create_engine(contract_id, db_url)
        return self._session_factories[contract_id]()

    async def close_all(self) -> None:
        """Dispose of all client database engines."""
        for contract_id, eng in self._engines.items():
            await eng.dispose()
            logger.info("client_db_disconnected", contract_id=str(contract_id))
        self._engines.clear()
        self._session_factories.clear()


# Singleton
client_db_manager = ClientDBManager()
