"""
Distributed backend surface (Phase 4): readiness/liveness probes,
correlation/trace/server-identity propagation, trusted-proxy IP
resolution, server identity, and the startup/shutdown lifecycle.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.core.server_identity import SERVER_IDENTITY
from app.main import app, lifespan
from app.utils.network import get_client_ip


# ---------------------------------------------------------------------
# /live, /ready
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_liveness_check_always_returns_200(client: AsyncClient):
    response = await client.get("/api/v1/live")
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["alive"] is True
    assert body["instance_id"] == SERVER_IDENTITY.instance_id


@pytest.mark.asyncio
async def test_readiness_check_returns_200_when_started(client: AsyncClient, monkeypatch):
    # DB connectivity here goes through the real engine (bound to Postgres
    # settings), not the SQLite `get_db` override — there's no real
    # Postgres in the test sandbox, so this test stubs the DB check the
    # same way the lifespan tests do, isolating "does /ready compute the
    # right response for a healthy fleet" from "is Postgres running."
    async def _healthy(*_a, **_kw):
        return True, 0.1

    monkeypatch.setattr("app.api.v1.health.routes.check_database_connection", _healthy)

    response = await client.get("/api/v1/ready")
    assert response.status_code == 200
    assert response.json()["data"]["ready"] is True


@pytest.mark.asyncio
async def test_readiness_check_returns_503_before_startup_completes(client: AsyncClient):
    app.state.ready = False
    try:
        response = await client.get("/api/v1/ready")
        assert response.status_code == 503
        assert response.json()["data"]["ready"] is False
        assert response.json()["success"] is False
    finally:
        app.state.ready = True


@pytest.mark.asyncio
async def test_readiness_check_returns_503_while_draining(client: AsyncClient):
    """Same code path as a graceful shutdown flipping `app.state.ready = False` mid-drain."""
    app.state.ready = False
    try:
        response = await client.get("/api/v1/ready")
        assert response.status_code == 503
    finally:
        app.state.ready = True


# ---------------------------------------------------------------------
# Correlation ID / trace ID / server ID propagation
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_correlation_id_is_generated_when_absent(client: AsyncClient):
    response = await client.get("/api/v1/live")
    assert response.headers["X-Correlation-ID"] == response.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_correlation_id_is_echoed_when_provided(client: AsyncClient):
    response = await client.get("/api/v1/live", headers={"X-Correlation-ID": "test-correlation-abc123"})
    assert response.headers["X-Correlation-ID"] == "test-correlation-abc123"
    # request_id is always freshly generated per-hop, distinct from the caller-supplied correlation id.
    assert response.headers["X-Request-ID"] != "test-correlation-abc123"


@pytest.mark.asyncio
async def test_trace_id_is_echoed_when_provided(client: AsyncClient):
    trace_id = "a" * 32
    response = await client.get("/api/v1/live", headers={"X-Trace-ID": trace_id})
    assert response.headers["X-Trace-ID"] == trace_id


@pytest.mark.asyncio
async def test_trace_id_is_generated_with_otel_shape_when_absent(client: AsyncClient):
    response = await client.get("/api/v1/live")
    trace_id = response.headers["X-Trace-ID"]
    assert len(trace_id) == 32
    int(trace_id, 16)  # must be valid hex


@pytest.mark.asyncio
async def test_server_id_header_matches_this_instance(client: AsyncClient):
    response = await client.get("/api/v1/live")
    assert response.headers["X-Server-ID"] == SERVER_IDENTITY.instance_id


@pytest.mark.asyncio
async def test_response_time_header_present_and_numeric(client: AsyncClient):
    response = await client.get("/api/v1/live")
    float(response.headers["X-Response-Time-Ms"])  # must not raise


@pytest.mark.asyncio
async def test_request_ids_differ_across_requests(client: AsyncClient):
    first = await client.get("/api/v1/live")
    second = await client.get("/api/v1/live")
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


# ---------------------------------------------------------------------
# Security headers still apply to every response, including our new routes
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_security_headers_present_on_ready_endpoint(client: AsyncClient):
    response = await client.get("/api/v1/ready")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


# ---------------------------------------------------------------------
# Trusted proxy client IP resolution
# ---------------------------------------------------------------------
class _FakeClient:
    def __init__(self, host: str):
        self.host = host


class _FakeRequest:
    def __init__(self, peer: str, headers: dict):
        self.client = _FakeClient(peer)
        self.headers = headers


def test_client_ip_untrusted_peer_ignores_forwarded_header():
    request = _FakeRequest("203.0.113.5", {"x-forwarded-for": "1.2.3.4"})
    assert get_client_ip(request, trusted_proxies=[]) == "203.0.113.5"


def test_client_ip_trusted_peer_uses_forwarded_header():
    request = _FakeRequest("10.0.0.1", {"x-forwarded-for": "203.0.113.9, 10.0.0.1"})
    assert get_client_ip(request, trusted_proxies=["10.0.0.1"]) == "203.0.113.9"


def test_client_ip_untrusted_peer_not_in_list_falls_back_to_peer():
    request = _FakeRequest("10.0.0.99", {"x-forwarded-for": "203.0.113.9"})
    assert get_client_ip(request, trusted_proxies=["10.0.0.1"]) == "10.0.0.99"


def test_client_ip_wildcard_trusts_any_peer():
    request = _FakeRequest("10.0.0.55", {"x-forwarded-for": "203.0.113.9"})
    assert get_client_ip(request, trusted_proxies=["*"]) == "203.0.113.9"


def test_client_ip_no_forwarded_header_falls_back_to_peer():
    request = _FakeRequest("10.0.0.1", {})
    assert get_client_ip(request, trusted_proxies=["10.0.0.1"]) == "10.0.0.1"


# ---------------------------------------------------------------------
# Startup / shutdown lifecycle
#
# `lifespan`'s DB check goes through the real `engine` (bound to
# Postgres settings that don't exist in the test environment), so these
# tests stub `app.main.check_database_connection` directly rather than
# spinning up a real Postgres just to prove the lifecycle's control flow.
# Redis goes through the real `fake_redis_client` fixture (already
# monkeypatches the one seam every Phase 4 Redis consumer uses) and GCS
# goes through `fake_gcs_client`, so those two exercise the actual
# check functions, not stubs.
# ---------------------------------------------------------------------
async def _fake_healthy_check(*_args, **_kwargs) -> tuple[bool, float]:
    return True, 0.1


@pytest.mark.asyncio
async def test_lifespan_marks_app_ready_after_successful_startup(fake_redis_client, fake_gcs_client, monkeypatch):
    monkeypatch.setattr("app.main.check_database_connection", _fake_healthy_check)
    monkeypatch.setattr("app.main.get_storage_client", lambda: fake_gcs_client)

    async with lifespan(app):
        assert app.state.ready is True
        assert app.state.active_requests == 0
    assert app.state.ready is False  # flipped back False on shutdown

    app.state.ready = True  # restore for any tests that run after this one


@pytest.mark.asyncio
async def test_lifespan_fails_fast_when_a_dependency_never_recovers(fake_redis_client, fake_gcs_client, monkeypatch):
    async def _always_unhealthy(*_args, **_kwargs) -> tuple[bool, float]:
        return False, 0.1

    monkeypatch.setattr("app.main.check_database_connection", _always_unhealthy)
    monkeypatch.setattr("app.main.get_storage_client", lambda: fake_gcs_client)
    monkeypatch.setattr("app.main.settings.DEPENDENCY_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr("app.main.settings.DEPENDENCY_RETRY_BACKOFF_SECONDS", 0.01)

    with pytest.raises(RuntimeError, match="Database is unreachable"):
        async with lifespan(app):
            pass  # pragma: no cover - startup must raise before yielding

    app.state.ready = True  # restore for any tests that run after this one


@pytest.mark.asyncio
async def test_lifespan_drains_in_flight_requests_before_reporting_shutdown_complete(
    fake_redis_client, fake_gcs_client, monkeypatch
):
    monkeypatch.setattr("app.main.check_database_connection", _fake_healthy_check)
    monkeypatch.setattr("app.main.get_storage_client", lambda: fake_gcs_client)
    monkeypatch.setattr("app.main.settings.GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS", 2.0)

    async with lifespan(app):
        app.state.active_requests = 1

        async def _finish_request_shortly():
            await asyncio.sleep(0.05)
            app.state.active_requests = 0

        asyncio.create_task(_finish_request_shortly())
        # Exiting the `async with` block here triggers the shutdown path,
        # which must wait for `_finish_request_shortly` to zero the
        # counter rather than tearing down pools immediately. If draining
        # didn't work, `active_requests` would still read 1 at the point
        # shutdown logging fires — we can't observe that directly here,
        # so the meaningful assertion is simply that this completes
        # within the loop's own poll cadence instead of hanging until the
        # full 2s timeout.
        pass

    assert app.state.active_requests == 0
    app.state.ready = True  # restore for subsequent tests
