"""
Redis client, connection pool, and distributed-primitives plumbing.

Design decisions:
- A single module-level `ConnectionPool` is created once and reused for
  the lifetime of the process; individual `Redis` client instances are
  cheap views over that pool, so we hand out a fresh client per request
  via dependency injection without re-establishing TCP connections.
- No *metadata* caching logic lives here yet (explicitly deferred past
  Phase 4). This module provides the shared-state plumbing three other
  Phase 4 features build on: distributed locks
  (`app/core/distributed_lock.py`), idempotency-key storage
  (`app/services/idempotency_service.py`), and health/readiness checks.
- `decode_responses=True` so callers get native `str` back instead of
  `bytes`, matching typical Python ergonomics.
- `socket_connect_timeout`/`socket_timeout` are set explicitly rather
  than left at the client default (which can block indefinitely) — in a
  distributed deployment, a hung Redis call must fail fast so the
  request can degrade gracefully (or the health check can report
  "unhealthy") instead of tying up a worker forever.
- `check_redis_connection` wraps the PING in `retry_async` so a single
  transient blip during a rolling Redis restart doesn't flip readiness
  to "not ready" and pull a perfectly healthy replica out of rotation.
"""

from collections.abc import AsyncGenerator

import redis.asyncio as redis

from app.core.config import get_settings
from app.core.retry import RetryExhaustedError, retry_async

settings = get_settings()

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


async def check_redis_connection(*, with_retry: bool = True) -> bool:
    """
    Used by health/readiness endpoints and startup verification.

    `with_retry=False` is used by the fast `/live` liveness probe, which
    intentionally does NOT retry — liveness must answer in microseconds
    and never block on a downstream dependency (that's what `/ready` is
    for). `with_retry=True` (the default) is used by `/health`, `/ready`,
    and startup, where absorbing one transient blip is worth the extra
    latency.
    """
    client = get_redis_client()
    try:
        if not with_retry:
            return await client.ping()

        async def _ping() -> bool:
            return await client.ping()

        try:
            return await retry_async(
                _ping,
                max_attempts=settings.RETRY_MAX_ATTEMPTS,
                base_delay=settings.RETRY_BASE_DELAY_SECONDS,
                max_delay=settings.RETRY_MAX_DELAY_SECONDS,
                operation_name="redis_ping",
            )
        except RetryExhaustedError:
            return False
    except Exception:
        return False
    finally:
        await client.aclose()


async def close_redis_pool() -> None:
    """Called from the application lifespan shutdown hook."""
    await redis_pool.disconnect()
