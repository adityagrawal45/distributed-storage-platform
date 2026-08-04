"""Tests for GET /api/v1/health."""

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

    assert body["success"] is True
    assert "status" in body["data"]
    assert "database" in body["data"]
    assert "redis" in body["data"]
    assert "storage" in body["data"]
    assert body["data"]["response_time_ms"] is not None
    assert body["data"]["server"]["app_version"]
    assert body["data"]["server"]["environment"]
    assert body["data"]["server"]["instance_id"]


@pytest.mark.asyncio
async def test_health_check_sets_distributed_tracing_headers(client: AsyncClient):
    response = await client.get("/api/v1/health")

    assert "X-Request-ID" in response.headers
    assert "X-Correlation-ID" in response.headers
    assert "X-Trace-ID" in response.headers
    assert "X-Server-ID" in response.headers
    assert "X-Response-Time-Ms" in response.headers
