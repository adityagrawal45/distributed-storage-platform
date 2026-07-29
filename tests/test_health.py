<<<<<<< HEAD
"""Tests for GET /api/v1/health."""

=======
>>>>>>> b62d862acc4e93e3c4a06e1dd0022682031f3115
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
<<<<<<< HEAD
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
=======
async def test_health(client: AsyncClient):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["data"]["status"] == "healthy"
    assert data["data"]["checks"]["database"] == "ok"
    assert data["data"]["checks"]["redis"] == "ok"
>>>>>>> b62d862acc4e93e3c4a06e1dd0022682031f3115
