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
    assert body["data"]["version"]
    assert body["data"]["environment"]
