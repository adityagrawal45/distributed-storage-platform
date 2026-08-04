"""
Redis client and connection pool.

Design decisions:
- A single module-level `ConnectionPool` is created once and reused for
  the lifetime of the process; individual `Redis` client instances are
  cheap views over that pool, so we hand out a fresh client per request
  via dependency injection without re-establishing TCP connections.
- Callers (this module's own functions, `app/services/cache_service.py`,
  `app/core/distributed_lock.py`, the idempotency/rate-limit middleware)
  MUST call `get_redis_client()` through the module attribute
  (`from app.database import redis as redis_db; redis_db.get_redis_client()`)
  rather than importing the bare function. This is what lets tests
  monkeypatch a single seam (`app.database.redis.get_redis_client`) to
  swap in `fakeredis` everywhere at once — see `tests/conftest.py`.
- No metadata caching logic lives here yet (explicitly deferred to a
  later phase — see README). This module only proves out connectivity
  and provides the shared plumbing (pool, DI, health check, retry) that
  the cache layer, distributed lock, and idempotency store are built on.
- `decode_responses=True` so callers get native `str` back instead of
  `bytes`, matching typical Python ergonomics.
"""

import time
from collections.abc import AsyncGenerator

import redis.asyncio as redis

from app.core.config import get_settings
from app.core.retry import retry_async
from app.logging.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)

redis_pool: redis.ConnectionPool = redis.ConnectionPool.from_url(
    settings.REDIS_URL,
    max_connections=settings.REDIS_MAX_CONNECTIONS,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_timeout=5,
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


async def ping_redis() -> bool:
    """Single-attempt connectivity check, no retry — used inside a retry loop by callers."""
    client = get_redis_client()
    try:
        return bool(await client.ping())
    finally:
        await client.aclose()


async def check_redis_connection(*, with_retry: bool = False) -> tuple[bool, float]:
    """
    Used by the health check endpoints to verify Redis connectivity.

    Returns `(healthy, latency_ms)`. `with_retry=True` (used at startup,
    where a container-orchestration cold-start race against Redis itself
    starting is common) retries with backoff before giving up;
    `with_retry=False` (used by `/health`/`/ready` request handlers) fails
    fast on the first error so a single flaky probe doesn't add latency to
    every health check.
    """
    start = time.perf_counter()
    try:
        if with_retry:
            await retry_async(
                ping_redis,
                attempts=settings.DEPENDENCY_RETRY_ATTEMPTS,
                base_delay=settings.DEPENDENCY_RETRY_BACKOFF_SECONDS,
                max_delay=settings.DEPENDENCY_RETRY_BACKOFF_MAX_SECONDS,
                retry_on=(redis.RedisError, OSError),
                operation_name="redis_connect",
            )
        else:
            await ping_redis()
        healthy = True
    except Exception as exc:
        logger.warning("redis_health_check_failed", error=str(exc))
        healthy = False
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    return healthy, latency_ms
