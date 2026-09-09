"""
Redis client, connection pool, and distributed-primitives plumbing.

Design decisions:
- A single module-level `ConnectionPool` is created once and reused for
  the lifetime of the process; individual `Redis` client instances are
  cheap views over that pool, so we hand out a fresh client per request
  via dependency injection without re-establishing TCP connections.
- This module owns the process's ONLY Redis connection pool. Phase 7 adds
  caching and rate limiting on top of it and deliberately does NOT create
  a second pool: one pool means one place to size, one place to observe,
  and one bounded ceiling on connections against Memorystore
  (`REDIS_MAX_CONNECTIONS` per replica x replica count). A dedicated
  "cache pool" would double that ceiling for no benefit, since all of
  these workloads are short, non-blocking commands on the same server.
- Consumers: distributed locks (`app/core/distributed_lock.py`),
  idempotency-key storage (`app/services/idempotency_service.py`),
  health/readiness checks, and — Phase 7 — `CacheService`
  (`app/services/cache_service.py`) and `RateLimiter`
  (`app/core/rate_limiter.py`).
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

from collections.abc import AsyncGenerator, Awaitable
from typing import Any, cast

import redis.asyncio as redis

from app.core.config import get_settings
from app.core.retry import RetryExhaustedError, retry_async

settings = get_settings()

redis_pool: redis.ConnectionPool = redis.ConnectionPool.from_url(
    settings.REDIS_URL,
    max_connections=settings.REDIS_MAX_CONNECTIONS,
    decode_responses=True,
    # Phase 7: these were hardcoded 5s literals; they are now
    # config-driven and tightened to 2s. Redis is on the critical path of
    # every cached read now, so a slow Redis must fail fast enough that
    # falling back to Postgres is still cheaper than waiting.
    socket_connect_timeout=settings.REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS,
    socket_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
    retry_on_timeout=settings.REDIS_RETRY_ON_TIMEOUT,
    # Proactively PING idle pooled connections rather than discovering a
    # silently-dropped connection (Memorystore maintenance, a NAT idle
    # timeout) on a user's request.
    health_check_interval=settings.REDIS_HEALTH_CHECK_INTERVAL_SECONDS,
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


async def eval_script(client: redis.Redis, script: str, numkeys: int, *keys_and_args: str) -> Any:
    """
    Typed wrapper around `Redis.eval` (Phase 12 mypy pass).

    `redis-py`'s installed type stub declares `eval`'s return as
    `Awaitable[str] | str` unconditionally — a real gap in the stub, not
    in this codebase, on two counts: (1) for `redis.asyncio.Redis` (the
    only client this project ever constructs — see `get_redis_client`
    above) the call always returns a coroutine, never a bare `str`; (2)
    a Lua script's actual return type depends entirely on what THAT
    script returns (an integer, a string, or — like
    `RateLimiter`'s `TOKEN_BUCKET_SCRIPT` — a 3-element table, which
    `redis-py` decodes as a `list`), so hardcoding `str` here would be
    just as wrong as the stub's own claim. `Any` is the honest return
    type; `cast` resolves mypy's "Incompatible types in await" by
    narrowing the AWAIT expression to what's actually true at runtime
    (a coroutine), without asserting anything false about what it
    resolves TO — callers still narrow the result themselves
    (`bool(...)`, `int(raw[0])`, etc.), exactly as before this wrapper
    existed. Every `Redis.eval` call site in this codebase goes through
    this one wrapper (`app/core/distributed_lock.py`,
    `app/core/rate_limiter.py`) instead of repeating the same `cast` at
    each call site.
    """
    return await cast("Awaitable[Any]", client.eval(script, numkeys, *keys_and_args))


async def close_redis_pool() -> None:
    """Called from the application lifespan shutdown hook."""
    await redis_pool.disconnect()
