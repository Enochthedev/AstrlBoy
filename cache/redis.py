"""
Upstash Redis client with distributed lock helpers.

Used for caching and distributed locks to prevent double execution
of scheduled jobs on Railway restart.
"""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import redis.asyncio as redis

from core.config import settings
from core.logging import get_logger

logger = get_logger("cache.redis")

# Singleton async Redis connection with proper timeouts.
# Without explicit timeouts, a network hiccup between Railway and Upstash
# causes the connection to hang indefinitely and kill the entire job.
redis_client: redis.Redis = redis.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_timeout=10,          # 10s timeout for individual commands
    socket_connect_timeout=10,  # 10s timeout for establishing connection
    retry_on_timeout=True,      # Auto-retry once on timeout
) if settings.redis_url else None  # type: ignore[assignment]


@asynccontextmanager
async def redis_lock(
    name: str,
    timeout: int = 300,
    blocking_timeout: int = 0,
) -> AsyncIterator[bool]:
    """Acquire a distributed Redis lock for the duration of the context.

    Used by scheduled jobs to prevent double execution when Railway
    restarts or scales. Non-blocking by default — if the lock is already
    held, the job silently skips.

    Args:
        name: Lock name (e.g. 'content_job').
        timeout: Lock auto-expires after this many seconds.
        blocking_timeout: How long to wait for the lock. 0 = don't wait.

    Yields:
        True if the lock was acquired, False if it was already held.
    """
    if redis_client is None:
        # No Redis configured — run without lock (dev/local mode)
        logger.warning("redis_not_configured", lock_name=name)
        yield True
        return

    lock = None
    try:
        lock = redis_client.lock(
            f"astrlboy:lock:{name}",
            timeout=timeout,
            blocking_timeout=blocking_timeout,
        )
        acquired = await lock.acquire(blocking=False)
    except (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError, OSError) as exc:
        # Redis is unreachable — run anyway rather than skipping the entire job.
        # The lock exists to prevent double execution, but a missed job is worse
        # than a rare double execution.
        logger.warning("redis_lock_unavailable", lock_name=name, error=str(exc))
        yield True
        return

    if not acquired:
        logger.info("lock_already_held", lock_name=name)
        yield False
        return

    try:
        logger.info("lock_acquired", lock_name=name)
        yield True
    finally:
        try:
            await lock.release()
            logger.info("lock_released", lock_name=name)
        except redis.exceptions.LockNotOwnedError:
            # Lock expired before we released — harmless
            logger.warning("lock_expired_before_release", lock_name=name)
        except (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError, OSError):
            # Redis went away during the job — lock will auto-expire
            logger.warning("redis_lock_release_failed", lock_name=name)


async def close_redis() -> None:
    """Close the Redis connection pool."""
    if redis_client is not None:
        await redis_client.aclose()
