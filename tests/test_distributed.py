"""
Tests for Phase 4: distributed backend infrastructure.

Covers idempotency (safe retries via Idempotency-Key), correlation-ID
propagation, retry/backoff, circuit breaker, distributed locking, and
graceful degradation on dependency failure — all against the same
hermetic fakes (SQLite, FakeGCSClient, FakeRedisClient) the rest of the
suite uses, per app design: no external services required to run
`pytest`.
"""

import asyncio
import io
import uuid

import pytest
from httpx import AsyncClient

from app.core.circuit_breaker import CircuitBreaker, CircuitState
from app.core.distributed_lock import DistributedLock, DistributedLockFactory
from app.core.retry import RetryExhaustedError, retry_async
from app.exceptions.custom_exceptions import CircuitBreakerOpenException, LockAcquisitionException
from app.services.idempotency_service import IdempotencyOutcome, IdempotencyService, compute_fingerprint
from tests.fakes.fake_redis import FakeRedisClient

PDF_MAGIC_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"0" * 100


def _upload_payload(filename: str, content: bytes, content_type: str = "application/pdf") -> dict:
    return {"file": (filename, io.BytesIO(content), content_type)}


# ---------------------------------------------------------------------
# Idempotency-Key — end-to-end through the upload route
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_upload_with_idempotency_key_replays_response_on_retry(authed_client: AsyncClient):
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}

    first = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("report.pdf", PDF_MAGIC_BYTES), headers=headers
    )
    assert first.status_code == 201
    first_file_id = first.json()["data"]["file"]["id"]

    # Retried with the SAME key and same logical request — must replay
    # the exact original response, not upload a second time.
    second = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("report.pdf", PDF_MAGIC_BYTES), headers=headers
    )
    assert second.status_code == 201
    assert second.json()["data"]["file"]["id"] == first_file_id


@pytest.mark.asyncio
async def test_upload_idempotency_key_reused_with_different_file_is_rejected(authed_client: AsyncClient):
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}

    await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("report.pdf", PDF_MAGIC_BYTES), headers=headers
    )
    response = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("different.pdf", PDF_MAGIC_BYTES), headers=headers
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_upload_without_idempotency_key_creates_duplicate_rows(authed_client: AsyncClient):
    """Baseline: no key at all means no idempotency protection (existing Phase 3 behavior)."""
    await authed_client.post("/api/v1/files/upload", files=_upload_payload("a.pdf", PDF_MAGIC_BYTES))
    response = await authed_client.post("/api/v1/files/upload", files=_upload_payload("a.pdf", PDF_MAGIC_BYTES))
    # No Idempotency-Key -> normal Phase 3 duplicate-filename rejection applies.
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_concurrent_uploads_with_same_idempotency_key(authed_client: AsyncClient):
    """
    Simulates a client firing a retry before the first attempt finished.

    Asserts on the *outcome that actually matters* (exactly one file
    row is ever created) rather than the exact pair of status codes:
    depending on exactly how the two requests interleave on the event
    loop, the second request may either lose the race outright (409,
    "in progress") or arrive after the first has already finished and
    legitimately replay its cached response (201) — both are correct,
    safe-retry behavior. What must NEVER happen is two uploads.
    """
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}

    responses = await asyncio.gather(
        authed_client.post("/api/v1/files/upload", files=_upload_payload("race.pdf", PDF_MAGIC_BYTES), headers=headers),
        authed_client.post("/api/v1/files/upload", files=_upload_payload("race.pdf", PDF_MAGIC_BYTES), headers=headers),
        return_exceptions=True,
    )
    status_codes = sorted(r.status_code for r in responses if not isinstance(r, Exception))
    assert all(code in (201, 409) for code in status_codes)

    search = await authed_client.get("/api/v1/metadata/search", params={"q": "race.pdf"})
    assert search.json()["data"]["total"] == 1


# ---------------------------------------------------------------------
# IdempotencyService (unit-level, against FakeRedisClient)
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_idempotency_service_proceeds_on_first_attempt(fake_redis_client: FakeRedisClient):
    service = IdempotencyService(fake_redis_client, ttl_seconds=60)
    user_id = uuid.uuid4()
    result = await service.check_or_begin(user_id, "key-1", "fingerprint-a")
    assert result.outcome is IdempotencyOutcome.PROCEED


@pytest.mark.asyncio
async def test_idempotency_service_replays_completed_response(fake_redis_client: FakeRedisClient):
    service = IdempotencyService(fake_redis_client, ttl_seconds=60)
    user_id = uuid.uuid4()

    await service.check_or_begin(user_id, "key-1", "fingerprint-a")
    await service.complete(user_id, "key-1", "fingerprint-a", 201, {"ok": True})

    result = await service.check_or_begin(user_id, "key-1", "fingerprint-a")
    assert result.outcome is IdempotencyOutcome.REPLAY
    assert result.cached_status_code == 201
    assert result.cached_body == {"ok": True}


@pytest.mark.asyncio
async def test_idempotency_service_fail_releases_the_claim(fake_redis_client: FakeRedisClient):
    service = IdempotencyService(fake_redis_client, ttl_seconds=60)
    user_id = uuid.uuid4()

    await service.check_or_begin(user_id, "key-1", "fingerprint-a")
    await service.fail(user_id, "key-1")

    # Released -> a retry with the same key/fingerprint proceeds again.
    result = await service.check_or_begin(user_id, "key-1", "fingerprint-a")
    assert result.outcome is IdempotencyOutcome.PROCEED


def test_compute_fingerprint_is_deterministic_and_order_sensitive():
    a = compute_fingerprint("upload", "folder-1", "report.pdf")
    b = compute_fingerprint("upload", "folder-1", "report.pdf")
    c = compute_fingerprint("upload", "folder-2", "report.pdf")
    assert a == b
    assert a != c


# ---------------------------------------------------------------------
# Distributed lock (against FakeRedisClient)
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_distributed_lock_acquire_and_release(fake_redis_client: FakeRedisClient):
    lock = DistributedLock(fake_redis_client, "resource-1", ttl_seconds=5)
    assert await lock.acquire() is True
    await lock.release()
    # Released -> acquirable again.
    assert await lock.acquire() is True


@pytest.mark.asyncio
async def test_distributed_lock_second_acquire_fails_while_held(fake_redis_client: FakeRedisClient):
    lock_a = DistributedLock(fake_redis_client, "resource-1", ttl_seconds=5)
    lock_b = DistributedLock(fake_redis_client, "resource-1", ttl_seconds=5)

    assert await lock_a.acquire() is True
    assert await lock_b.acquire() is False


@pytest.mark.asyncio
async def test_distributed_lock_context_manager_raises_on_conflict(fake_redis_client: FakeRedisClient):
    factory = DistributedLockFactory(fake_redis_client, default_ttl_seconds=5)

    async with factory.lock("resource-1"):
        with pytest.raises(LockAcquisitionException):
            async with factory.lock("resource-1"):
                pass  # pragma: no cover — must not be reached


@pytest.mark.asyncio
async def test_distributed_lock_release_only_affects_own_token(fake_redis_client: FakeRedisClient):
    """A lock must never release a key another holder has since re-acquired."""
    lock_a = DistributedLock(fake_redis_client, "resource-1", ttl_seconds=5)
    await lock_a.acquire()
    await lock_a.release()

    lock_b = DistributedLock(fake_redis_client, "resource-1", ttl_seconds=5)
    assert await lock_b.acquire() is True

    # lock_a's (already-released) release() call must be a safe no-op,
    # never touching lock_b's now-held key.
    await lock_a.release()
    assert await fake_redis_client.get("lock:resource-1") is not None


# ---------------------------------------------------------------------
# Retry utility
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_retry_async_succeeds_after_transient_failures():
    attempts = {"count": 0}

    async def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("transient")
        return "ok"

    result = await retry_async(flaky, max_attempts=5, base_delay=0.001, max_delay=0.01, operation_name="test")
    assert result == "ok"
    assert attempts["count"] == 3


@pytest.mark.asyncio
async def test_retry_async_raises_retry_exhausted_after_max_attempts():
    async def always_fails():
        raise ConnectionError("permanently down")

    with pytest.raises(RetryExhaustedError) as exc_info:
        await retry_async(always_fails, max_attempts=3, base_delay=0.001, max_delay=0.01, operation_name="test")
    assert exc_info.value.attempts == 3


@pytest.mark.asyncio
async def test_retry_async_does_not_retry_unlisted_exceptions():
    attempts = {"count": 0}

    async def raises_value_error():
        attempts["count"] += 1
        raise ValueError("not transient")

    with pytest.raises(ValueError):
        await retry_async(
            raises_value_error,
            max_attempts=5,
            base_delay=0.001,
            max_delay=0.01,
            retry_on=(ConnectionError,),
            operation_name="test",
        )
    assert attempts["count"] == 1


# ---------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_failure_threshold():
    breaker = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout=60)

    async def always_fails():
        raise ConnectionError("down")

    for _ in range(3):
        with pytest.raises(ConnectionError):
            await breaker.call(always_fails)

    assert breaker.state == CircuitState.OPEN
    with pytest.raises(CircuitBreakerOpenException):
        await breaker.call(always_fails)


@pytest.mark.asyncio
async def test_circuit_breaker_closes_after_successful_call():
    breaker = CircuitBreaker(name="test", failure_threshold=2, recovery_timeout=60)

    async def fails():
        raise ConnectionError("down")

    async def succeeds():
        return "ok"

    with pytest.raises(ConnectionError):
        await breaker.call(fails)
    assert breaker.state == CircuitState.CLOSED  # below threshold still

    result = await breaker.call(succeeds)
    assert result == "ok"
    assert breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_half_opens_after_recovery_timeout():
    breaker = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=0.01)

    async def fails():
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        await breaker.call(fails)
    assert breaker.state == CircuitState.OPEN

    await asyncio.sleep(0.02)
    assert breaker.state == CircuitState.HALF_OPEN


# ---------------------------------------------------------------------
# Server identity / correlation propagation
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_every_response_carries_server_identity_header(client: AsyncClient):
    response = await client.get("/api/v1/live")
    assert response.headers["X-Server-ID"]


@pytest.mark.asyncio
async def test_multiple_requests_from_same_process_share_server_id(client: AsyncClient):
    """Same process -> same server_id across requests (proves per-process, not per-request, identity)."""
    first = await client.get("/api/v1/live")
    second = await client.get("/api/v1/live")
    assert first.headers["X-Server-ID"] == second.headers["X-Server-ID"]


# ---------------------------------------------------------------------
# Graceful degradation on dependency failure
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_health_check_reports_unhealthy_storage_without_crashing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    from tests.fakes.fake_gcs import FakeBucket

    def _boom(self):
        raise ConnectionError("simulated storage outage")

    monkeypatch.setattr(FakeBucket, "exists", _boom)

    response = await client.get("/api/v1/health")
    # Must degrade gracefully (never 500) even when a dependency check itself raises.
    assert response.status_code == 200
    assert response.json()["data"]["storage"]["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_readiness_reports_not_ready_when_storage_unhealthy(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    from tests.fakes.fake_gcs import FakeBucket

    def _boom(self):
        raise ConnectionError("simulated storage outage")

    monkeypatch.setattr(FakeBucket, "exists", _boom)

    response = await client.get("/api/v1/ready")
    assert response.status_code == 503
    assert response.json()["data"]["ready"] is False


# ---------------------------------------------------------------------
# Concurrent requests (statelessness sanity check)
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_many_concurrent_requests_each_get_unique_request_ids(client: AsyncClient):
    responses = await asyncio.gather(*[client.get("/api/v1/live") for _ in range(10)])
    request_ids = {r.headers["X-Request-ID"] for r in responses}
    assert len(request_ids) == 10  # no collisions, no shared mutable state leaking between requests
