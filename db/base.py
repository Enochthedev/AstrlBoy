"""
SQLAlchemy async engine and session factory.

All database access goes through the session factory created here.
Sessions are always used as async context managers to ensure cleanup.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from core.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""

    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an async session, ensuring it is closed after use.

    Yields:
        An AsyncSession bound to the primary database engine.
    """
    async with async_session_factory() as session:
        yield session


async def close_engine() -> None:
    """Dispose of the async engine and all pooled connections."""
    await engine.dispose()
