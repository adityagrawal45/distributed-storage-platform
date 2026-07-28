import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_register(client: AsyncClient):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "test@example.com",
            "first_name": "Test",
            "last_name": "User",
            "password": "TestPass123!",
        }
    )
    assert response.status_code == 201
    data = response.json()
    assert data["success"] is True
    assert "access_token" in data["data"]
    assert "refresh_token" in data["data"]


@pytest.mark.asyncio
async def test_login(client: AsyncClient, db_session):
    # First register
    await client.post("/api/v1/auth/register", json={
        "email": "login@example.com",
        "first_name": "Login",
        "last_name": "Test",
        "password": "LoginPass123!",
    })
    response = await client.post("/api/v1/auth/login", json={
        "email": "login@example.com",
        "password": "LoginPass123!",
    })
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "access_token" in data["data"]


@pytest.mark.asyncio
async def test_protected_endpoint(client: AsyncClient):
    # Register and get token
    reg = await client.post("/api/v1/auth/register", json={
        "email": "protected@example.com",
        "first_name": "Protected",
        "last_name": "User",
        "password": "TestPass123!",
    })
    token = reg.json()["data"]["access_token"]
    response = await client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["data"]["email"] == "protected@example.com"