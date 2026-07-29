"""Tests for POST /api/v1/auth/register."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_register_success(client: AsyncClient, valid_user_payload: dict):
    response = await client.post("/api/v1/auth/register", json=valid_user_payload)

    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["data"]["email"] == valid_user_payload["email"]
    assert body["data"]["role"] == "user"
    assert body["data"]["is_active"] is True
    assert "hashed_password" not in body["data"]
    assert "password" not in body["data"]


@pytest.mark.asyncio
async def test_register_duplicate_email_returns_409(client: AsyncClient, valid_user_payload: dict):
    await client.post("/api/v1/auth/register", json=valid_user_payload)
    response = await client.post("/api/v1/auth/register", json=valid_user_payload)

    assert response.status_code == 409
    body = response.json()
    assert body["success"] is False


@pytest.mark.asyncio
async def test_register_weak_password_returns_422(client: AsyncClient, valid_user_payload: dict):
    valid_user_payload["password"] = "weak"
    response = await client.post("/api/v1/auth/register", json=valid_user_payload)

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["errors"]


@pytest.mark.asyncio
async def test_register_invalid_email_returns_422(client: AsyncClient, valid_user_payload: dict):
    valid_user_payload["email"] = "not-an-email"
    response = await client.post("/api/v1/auth/register", json=valid_user_payload)

    assert response.status_code == 422
