"""
LangGraph checkpointer backed by Neon PostgreSQL.

Enables graph state persistence and resume-on-failure. When a graph run
fails mid-way, it resumes from the last checkpoint instead of starting over.

Uses langgraph-checkpoint-postgres which requires psycopg (not asyncpg),
so we derive a plain postgres:// URL from the existing DATABASE_URL.
"""

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from core.config import settings
from core.logging import get_logger

logger = get_logger("db.checkpointer")


def _to_psycopg_url(url: str) -> str:
    """Convert an asyncpg-style URL to a plain psycopg-compatible URL.

    Strips the +asyncpg driver suffix since psycopg uses its own connection
    handling. Also strips Neon query params that psycopg doesn't need.

    Args:
        url: A database URL, potentially with +asyncpg driver.

    Returns:
        A plain postgresql:// URL suitable for psycopg.
    """
    from urllib.parse import urlparse, urlunparse

    clean = url.replace("postgresql+asyncpg://", "postgresql://")
    clean = clean.replace("postgres+asyncpg://", "postgresql://")
    if not clean.startswith("postgresql://"):
        clean = clean.replace("postgres://", "postgresql://", 1)

    # Strip query params — Neon adds sslmode, channel_binding, etc.
    parsed = urlparse(clean)
    if parsed.query:
        clean = urlunparse(parsed._replace(query=""))

    return clean


_psycopg_url = _to_psycopg_url(settings.database_url)

# Track whether setup has been called to avoid redundant migrations
_setup_done = False


async def get_checkpointer() -> AsyncPostgresSaver:
    """Return a LangGraph checkpointer backed by Neon PostgreSQL.

    Creates the checkpoint tables on first call. Subsequent calls skip setup.

    Returns:
        An AsyncPostgresSaver instance ready to use with graph.compile().
    """
    global _setup_done

    checkpointer = AsyncPostgresSaver.from_conn_string(_psycopg_url)

    if not _setup_done:
        await checkpointer.setup()
        _setup_done = True
        logger.info("checkpointer_setup_complete")

    return checkpointer
