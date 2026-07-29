"""
Redis client and connection pool.

Design decisions:
- A single module-level `ConnectionPool` is created once and reused for
  the lifetime of the process; individual `Redis` client instances are
  cheap views over that pool, so we hand out a fresh client per request
  via dependency injection without re-establishing TCP connections.
- No caching logic lives here yet (Phase 1 explicitly defers caching).
  This module only proves out connectivity and provides the plumbing
  future phases (rate limiting, caching, pub/sub) will build on.
- `decode_responses=True` so callers get native `str` back instead of
  `bytes`, matching typical Python ergonomics.
"""

from collections.abc import AsyncGenerator

import redis.asyncio as redis

from app.core.config import get_settings

settings = get_settings()

redis_pool: redis.ConnectionPool = redis.ConnectionPool.from_url(
    settings.REDIS_URL,
    max_connections=settings.REDIS_MAX_CONNECTIONS,
    decode_responses=True,
)


def get_redis_client() -> redis.Redis:
    """Return a Redis client bound to the shared connection pool."""
    return redis.Redis(connection_pool=redis_pool)


async def get_redis() -> AsyncGenerator[redis.Redis, None]:
    """FastAPI dependency yielding a Redis client for the request."""
    client = get_redis_client()
    try:
        yield client
    finally:
        await client.aclose()


async def check_redis_connection() -> bool:
    """Used by the health check endpoint to verify Redis connectivity."""
    client = get_redis_client()
    try:
        return await client.ping()
    except Exception:
        return False
    finally:
        await client.aclose()
