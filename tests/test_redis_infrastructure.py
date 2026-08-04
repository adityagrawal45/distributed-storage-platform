"""
Redis infrastructure (Phase 4): shared cache layer, distributed lock,
connection health check, retry helper, circuit breaker.

Uses the `fake_redis_client` fixture (see tests/conftest.py) — hermetic,
no real Redis process required, mirroring how Phase 3 tests never touch
real GCS.
"""

import asyncio

import pytest
import redis.asyncio as redis_asyncio

from app.core.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState
from app.core.distributed_lock import DistributedLock
from app.core.retry import RetryExhaustedError, retry_async
from app.exceptions.custom_exceptions import LockAcquisitionException
from app.services.cache_service import CacheService

pytestmark = pytest.mark.usefixtures("fake_redis_client")


# ---------------------------------------------------------------------
# CacheService
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cache_set_and_get_roundtrip():
    cache = CacheService()
    assert await cache.set("greeting", "hello", ttl_seconds=60) is True
    assert await cache.get("greeting") == "hello"


@pytest.mark.asyncio
async def test_cache_get_missing_key_returns_none():
    cache = CacheService()
    assert await cache.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_cache_json_roundtrip():
    cache = CacheService()
    await cache.set_json("obj", {"a": 1, "b": [1, 2, 3]})
    assert await cache.get_json("obj") == {"a": 1, "b": [1, 2, 3]}


@pytest.mark.asyncio
async def test_cache_delete():
    cache = CacheService()
    await cache.set("k", "v")
    assert await cache.delete("k") is True
    assert await cache.get("k") is None


@pytest.mark.asyncio
async def test_cache_exists():
    cache = CacheService()
    assert await cache.exists("k2") is False
    await cache.set("k2", "v")
    assert await cache.exists("k2") is True


@pytest.mark.asyncio
async def test_cache_increment_creates_and_increments():
    cache = CacheService()
    assert await cache.increment("counter", ttl_seconds=60) == 1
    assert await cache.increment("counter", ttl_seconds=60) == 2


@pytest.mark.asyncio
async def test_cache_degrades_gracefully_when_redis_unavailable(monkeypatch):
    """A cache miss/no-op, never an exception, when Redis is down — see CacheService's docstring."""

    class _BrokenRedis:
        async def get(self, *_a, **_kw):
            raise redis_asyncio.ConnectionError("simulated outage")

        async def aclose(self):
            return None

    import app.database.redis as redis_db

    monkeypatch.setattr(redis_db, "get_redis_client", lambda: _BrokenRedis())

    cache = CacheService(breaker=CircuitBreaker(name="test", failure_threshold=99, reset_timeout_seconds=30))
    assert await cache.get("anything") is None


# ---------------------------------------------------------------------
# DistributedLock
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_lock_acquire_and_release():
    lock = DistributedLock("resource-1", ttl_seconds=5)
    assert await lock.acquire() is True
    assert await lock.release() is True


@pytest.mark.asyncio
async def test_lock_prevents_concurrent_acquisition():
    lock_a = DistributedLock("resource-2", ttl_seconds=5)
    lock_b = DistributedLock("resource-2", ttl_seconds=5)

    assert await lock_a.acquire() is True
    assert await lock_b.acquire() is False  # already held

    await lock_a.release()
    assert await lock_b.acquire() is True  # free again
    await lock_b.release()


@pytest.mark.asyncio
async def test_lock_release_only_by_holder_token():
    """A lock can't be released by someone who never held it (no token) — a no-op, not an error."""
    lock_a = DistributedLock("resource-3", ttl_seconds=5)
    await lock_a.acquire()

    imposter = DistributedLock("resource-3", ttl_seconds=5)  # never called acquire()
    assert await imposter.release() is False

    # Still held by the real owner.
    lock_b = DistributedLock("resource-3", ttl_seconds=5)
    assert await lock_b.acquire() is False


@pytest.mark.asyncio
async def test_lock_context_manager_releases_on_exception():
    lock = DistributedLock("resource-4", ttl_seconds=5)
    with pytest.raises(ValueError):
        async with lock:
            raise ValueError("boom")

    # Lock must be free again after the context exits, even on error.
    lock2 = DistributedLock("resource-4", ttl_seconds=5)
    assert await lock2.acquire() is True


@pytest.mark.asyncio
async def test_lock_context_manager_raises_when_already_held():
    holder = DistributedLock("resource-5", ttl_seconds=5)
    await holder.acquire()

    with pytest.raises(LockAcquisitionException):
        async with DistributedLock("resource-5", ttl_seconds=5):
            pass  # pragma: no cover - should never enter


@pytest.mark.asyncio
async def test_lock_extend_renews_ttl_for_holder_only():
    lock = DistributedLock("resource-6", ttl_seconds=5)
    await lock.acquire()
    assert await lock.extend(additional_seconds=10) is True

    stranger = DistributedLock("resource-6")
    assert await stranger.extend() is False  # never acquired -> no token


# ---------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_retry_async_succeeds_after_transient_failures():
    calls = {"count": 0}

    async def flaky():
        calls["count"] += 1
        if calls["count"] < 3:
            raise ConnectionError("simulated transient failure")
        return "ok"

    result = await retry_async(flaky, attempts=5, base_delay=0.001, retry_on=(ConnectionError,))
    assert result == "ok"
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_retry_async_raises_after_exhausting_attempts():
    async def always_fails():
        raise ConnectionError("still down")

    with pytest.raises(RetryExhaustedError) as exc_info:
        await retry_async(always_fails, attempts=3, base_delay=0.001, retry_on=(ConnectionError,))
    assert exc_info.value.attempts == 3


@pytest.mark.asyncio
async def test_retry_async_does_not_retry_unlisted_exceptions():
    async def type_error():
        raise TypeError("a real bug, not a transient failure")

    with pytest.raises(TypeError):
        await retry_async(type_error, attempts=5, base_delay=0.001, retry_on=(ConnectionError,))


# ---------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------
def test_circuit_breaker_opens_after_threshold_failures():
    breaker = CircuitBreaker(name="test", failure_threshold=3, reset_timeout_seconds=30)
    assert breaker.state == CircuitState.CLOSED

    for _ in range(3):
        breaker.record_failure()

    assert breaker.state == CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        breaker.guard()


def test_circuit_breaker_closes_on_success():
    breaker = CircuitBreaker(name="test", failure_threshold=1, reset_timeout_seconds=30)
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN

    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED
    breaker.guard()  # must not raise


def test_circuit_breaker_half_opens_after_timeout():
    import time as time_module

    breaker = CircuitBreaker(name="test", failure_threshold=1, reset_timeout_seconds=0.05)
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN

    time_module.sleep(0.06)
    assert breaker.state == CircuitState.HALF_OPEN


# ---------------------------------------------------------------------
# Redis health check
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_check_redis_connection_healthy_with_fake_redis():
    from app.database.redis import check_redis_connection

    healthy, latency_ms = await check_redis_connection()
    assert healthy is True
    assert latency_ms >= 0
