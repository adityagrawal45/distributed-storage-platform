"""
Shared cache layer (infrastructure only — see README).

Design decisions:
- This is deliberately generic (`get`/`set`/`delete`/`exists`/`increment`
  on arbitrary string keys) and knows nothing about files, folders, or
  users. Phase 4's brief is explicit: "prepare Redis infrastructure, do
  NOT implement metadata caching yet." A future phase adds a
  `MetadataCacheService` (or similar) that calls into this one with
  actual cache keys/invalidation policy — that decision doesn't belong
  here.
- Every method degrades gracefully instead of raising: if Redis is down,
  a cache `get` behaves like a cache miss (`None`) and a cache `set`
  behaves like a no-op, rather than turning an optional optimization into
  a hard outage. This is the "Graceful Degradation" requirement from
  README's Error Handling section — correctness never depends on the
  cache being available, so failing open is always safe here (contrast
  with the idempotency store and distributed lock, which fail CLOSED —
  see their own docstrings for why).
- A per-process `CircuitBreaker` (see `app.core.circuit_breaker`) avoids
  hammering a downed Redis with a fresh TCP attempt (and its connect
  timeout) on every single request; once it trips, calls short-circuit
  to the graceful-degradation path immediately.
- Values are JSON-encoded so callers can cache arbitrary
  JSON-serializable Python objects, not just strings.
"""

import json
from typing import Any

import redis.asyncio as redis_asyncio

from app.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.core.config import get_settings
from app.database import redis as redis_db
from app.logging.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)

_breaker = CircuitBreaker(
    name="redis_cache",
    failure_threshold=settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    reset_timeout_seconds=settings.CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS,
)


class CacheService:
    """Thin, resilient wrapper over the shared Redis connection pool."""

    def __init__(self, breaker: CircuitBreaker = _breaker):
        self._breaker = breaker

    async def get(self, key: str) -> str | None:
        try:
            self._breaker.guard()
        except CircuitOpenError:
            return None

        client = redis_db.get_redis_client()
        try:
            value = await client.get(key)
            self._breaker.record_success()
            return value
        except redis_asyncio.RedisError as exc:
            logger.warning("cache_get_failed", key=key, error=str(exc))
            self._breaker.record_failure()
            return None
        finally:
            await client.aclose()

    async def get_json(self, key: str) -> Any | None:
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("cache_get_json_malformed", key=key)
            return None

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> bool:
        try:
            self._breaker.guard()
        except CircuitOpenError:
            return False

        client = redis_db.get_redis_client()
        try:
            await client.set(key, value, ex=ttl_seconds)
            self._breaker.record_success()
            return True
        except redis_asyncio.RedisError as exc:
            logger.warning("cache_set_failed", key=key, error=str(exc))
            self._breaker.record_failure()
            return False
        finally:
            await client.aclose()

    async def set_json(self, key: str, value: Any, ttl_seconds: int | None = None) -> bool:
        return await self.set(key, json.dumps(value), ttl_seconds=ttl_seconds)

    async def delete(self, key: str) -> bool:
        try:
            self._breaker.guard()
        except CircuitOpenError:
            return False

        client = redis_db.get_redis_client()
        try:
            await client.delete(key)
            self._breaker.record_success()
            return True
        except redis_asyncio.RedisError as exc:
            logger.warning("cache_delete_failed", key=key, error=str(exc))
            self._breaker.record_failure()
            return False
        finally:
            await client.aclose()

    async def exists(self, key: str) -> bool:
        try:
            self._breaker.guard()
        except CircuitOpenError:
            return False

        client = redis_db.get_redis_client()
        try:
            result = bool(await client.exists(key))
            self._breaker.record_success()
            return result
        except redis_asyncio.RedisError as exc:
            logger.warning("cache_exists_failed", key=key, error=str(exc))
            self._breaker.record_failure()
            return False
        finally:
            await client.aclose()

    async def increment(self, key: str, amount: int = 1, ttl_seconds: int | None = None) -> int | None:
        """Atomic counter — the building block for the rate-limit placeholder."""
        try:
            self._breaker.guard()
        except CircuitOpenError:
            return None

        client = redis_db.get_redis_client()
        try:
            new_value = await client.incrby(key, amount)
            if ttl_seconds is not None and new_value == amount:
                # Only the request that created the counter sets its expiry,
                # so concurrent incrementers never reset the window.
                await client.expire(key, ttl_seconds)
            self._breaker.record_success()
            return new_value
        except redis_asyncio.RedisError as exc:
            logger.warning("cache_increment_failed", key=key, error=str(exc))
            self._breaker.record_failure()
            return None
        finally:
            await client.aclose()
