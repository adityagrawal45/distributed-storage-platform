"""Tests for POST /api/v1/auth/login."""

import pytest
from httpx import AsyncClient


async def _register(client: AsyncClient, payload: dict):
    return await client.post("/api/v1/auth/register", json=payload)


@pytest.mark.asyncio
async def test_login_success(client: AsyncClient, valid_user_payload: dict):
    await _register(client, valid_user_payload)

    response = await client.post(
        "/api/v1/auth/login",
        data={"username": valid_user_payload["email"], "password": valid_user_payload["password"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert "access_token" in body["data"]
    assert "refresh_token" in body["data"]
    assert body["data"]["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_login_wrong_password_returns_401(client: AsyncClient, valid_user_payload: dict):
    await _register(client, valid_user_payload)

    response = await client.post(
        "/api/v1/auth/login",
        data={"username": valid_user_payload["email"], "password": "WrongPassword1!"},
    )

    assert response.status_code == 401
    assert response.json()["success"] is False


@pytest.mark.asyncio
async def test_login_nonexistent_user_returns_401(client: AsyncClient):
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": "ghost@nimbusfs.io", "password": "WhateverP@ss1"},
    )

    assert response.status_code == 401
