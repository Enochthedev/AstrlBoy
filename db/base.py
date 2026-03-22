"""
SQLAlchemy async engine and session factory.

All database access goes through the session factory created here.
Sessions are always used as async context managers to ensure cleanup.
"""

import ssl as _ssl_module
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from core.config import settings


def _normalize_db_url(url: str) -> str:
    """Ensure the URL uses the asyncpg driver and strip Neon query params.

    Neon URLs include params like sslmode, channel_binding, options that
    asyncpg doesn't accept. We strip all query params and handle SSL
    via connect_args instead.
    """
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    # Strip all query params — Neon adds sslmode, channel_binding, etc.
    # that asyncpg doesn't understand. SSL is handled via connect_args.
    parsed = urlparse(url)
    if parsed.query:
        url = urlunparse(parsed._replace(query=""))
    return url


def _ssl_connect_args(url: str) -> dict:
    """Return connect_args with SSL context.

    Neon always requires SSL. We detect this from the hostname (.neon.tech)
    or from sslmode/ssl in the original URL query string.
    """
    needs_ssl = (
        "sslmode=" in url
        or "ssl=" in url
        or ".neon.tech" in url
    )
    if needs_ssl:
        ctx = _ssl_module.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl_module.CERT_NONE
        return {"ssl": ctx}
    return {}


_raw_url = settings.database_url

engine = create_async_engine(
    _normalize_db_url(_raw_url),
    echo=False,
    pool_size=10,
    max_overflow=20,
    connect_args=_ssl_connect_args(_raw_url),
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
