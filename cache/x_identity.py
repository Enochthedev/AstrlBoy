"""
Cached X (Twitter) identity — avoids paying $0.01 per get_me() call.

The authenticated user's ID and username never change between deploys.
We call get_me() once at startup, cache the result in Redis (with a 24h TTL
as a safety net), and serve it from memory for the rest of the process lifetime.

Before this: ~15 get_me() calls/day = $4.50/month wasted.
After this: 1 call per deploy = ~$0.30/month.
"""

import tweepy

from cache.redis import redis_client
from core.config import settings
from core.logging import get_logger

logger = get_logger("cache.x_identity")

_REDIS_KEY_ID = "astrlboy:x:user_id"
_REDIS_KEY_USERNAME = "astrlboy:x:username"
_TTL = 86400  # 24h — refreshed on every deploy anyway

# In-memory cache for the process lifetime
_cached_user_id: str | None = None
_cached_username: str | None = None


async def get_x_user_id() -> str:
    """Get @astrlboy_'s X user ID without calling the API.

    Returns the cached user ID from memory, Redis, or falls back to
    a single API call if neither is available.

    Returns:
        The authenticated user's X ID as a string.

    Raises:
        RuntimeError: If the identity cannot be resolved from any source.
    """
    global _cached_user_id, _cached_username

    # 1. Check in-memory cache first (free)
    if _cached_user_id:
        return _cached_user_id

    # 2. Check Redis (free — no X API call)
    if redis_client is not None:
        try:
            uid = await redis_client.get(_REDIS_KEY_ID)
            username = await redis_client.get(_REDIS_KEY_USERNAME)
            if uid:
                _cached_user_id = uid if isinstance(uid, str) else uid.decode()
                _cached_username = (username if isinstance(username, str) else username.decode()) if username else None
                logger.info("x_identity_loaded_from_redis", user_id=_cached_user_id)
                return _cached_user_id
        except Exception:
            pass

    # 3. Last resort — call get_me() once and cache everywhere
    await _refresh_from_api()

    if _cached_user_id is None:
        raise RuntimeError("Could not resolve X user identity from any source")

    return _cached_user_id


async def get_x_username() -> str:
    """Get @astrlboy_'s X username without calling the API."""
    global _cached_username
    if not _cached_username:
        await get_x_user_id()  # populates both
    return _cached_username or settings.agent_handle.lstrip("@")


async def warm_cache() -> None:
    """Call once at startup to prime the identity cache.

    Makes a single get_me() call and stores the result in both
    in-memory and Redis. All subsequent calls are free.
    """
    try:
        await _refresh_from_api()
        logger.info(
            "x_identity_warmed",
            user_id=_cached_user_id,
            username=_cached_username,
        )
    except Exception as exc:
        logger.warning("x_identity_warm_failed", error=str(exc))


async def _refresh_from_api() -> None:
    """Make a single get_me() call and cache the result."""
    global _cached_user_id, _cached_username

    client = tweepy.Client(
        bearer_token=settings.twitter_bearer_token,
        consumer_key=settings.twitter_api_key,
        consumer_secret=settings.twitter_api_secret,
        access_token=settings.twitter_access_token,
        access_token_secret=settings.twitter_access_secret,
    )

    me = client.get_me()
    if not me or not me.data:
        logger.error("x_identity_api_failed", error="get_me returned no data")
        return

    _cached_user_id = str(me.data.id)
    _cached_username = me.data.username

    # Persist to Redis so restarts within 24h don't need another API call
    if redis_client is not None:
        try:
            pipe = redis_client.pipeline()
            pipe.set(_REDIS_KEY_ID, _cached_user_id, ex=_TTL)
            pipe.set(_REDIS_KEY_USERNAME, _cached_username, ex=_TTL)
            await pipe.execute()
        except Exception:
            pass  # In-memory cache still works

    logger.info("x_identity_refreshed", user_id=_cached_user_id, username=_cached_username)
