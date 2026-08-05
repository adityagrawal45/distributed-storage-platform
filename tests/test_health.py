"""
Tests for GET /api/v1/health, /api/v1/ready, /api/v1/live (Phase 4).

Note: `/health` and `/ready` check real DB/Redis/Storage connectivity
(unlike the rest of the test suite, which runs against SQLite + a fake
GCS client — see conftest.py). These tests deliberately assert only on
*response shape*, not on a specific "healthy" status, so the suite stays
green whether or not real Postgres/Redis/GCS happen to be reachable
from wherever `pytest` runs — the FastAPI dependency `db_session`/
`fake_gcs_client` fixtures used elsewhere don't apply to these routes'
direct infra calls by design (see app/database/session.py and
app/database/redis.py: health checks intentionally hit the real
process-wide engine/pool, not a request-scoped override, so `/health`
reports the truth about this replica's actual connectivity).
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_check_returns_200(client: AsyncClient):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_check_response_shape(client: AsyncClient):
    response = await client.get("/api/v1/health")
    body = response.json()

    assert "status" in body["data"]
    assert body["data"]["status"] in ("healthy", "degraded", "unhealthy")
    for component in ("database", "redis", "storage"):
        assert component in body["data"]
        assert body["data"][component]["status"] in ("healthy", "unhealthy")
    assert body["data"]["server"]["instance_id"]
    assert body["data"]["server"]["hostname"]
    assert body["data"]["server"]["app_version"]
    assert body["data"]["server"]["environment"]
    assert body["data"]["server"]["build_version"]
    assert isinstance(body["data"]["response_time_ms"], (int, float))


@pytest.mark.asyncio
async def test_readiness_check_returns_valid_shape(client: AsyncClient):
    response = await client.get("/api/v1/ready")
    assert response.status_code in (200, 503)
    body = response.json()
    assert isinstance(body["data"]["ready"], bool)
    assert body["success"] == body["data"]["ready"]
    assert body["data"]["server"]["instance_id"]


@pytest.mark.asyncio
async def test_liveness_check_returns_200_without_dependency_checks(client: AsyncClient):
    """Liveness never depends on DB/Redis/Storage — always 200 if the process can respond at all."""
    response = await client.get("/api/v1/live")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["alive"] is True
    assert body["data"]["server"]["instance_id"]


@pytest.mark.asyncio
async def test_health_response_includes_correlation_headers(client: AsyncClient):
    response = await client.get("/api/v1/live")
    assert "X-Request-ID" in response.headers
    assert "X-Correlation-ID" in response.headers
    assert "X-Trace-ID" in response.headers
    assert "X-Server-ID" in response.headers
    assert "X-Response-Time-Ms" in response.headers


@pytest.mark.asyncio
async def test_correlation_id_is_honored_from_request_header(client: AsyncClient):
    response = await client.get("/api/v1/live", headers={"X-Correlation-ID": "test-correlation-123"})
    assert response.headers["X-Correlation-ID"] == "test-correlation-123"


@pytest.mark.asyncio
async def test_correlation_id_generated_fresh_per_request_when_absent(client: AsyncClient):
    first = await client.get("/api/v1/live")
    second = await client.get("/api/v1/live")
    assert first.headers["X-Correlation-ID"] != second.headers["X-Correlation-ID"]
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
