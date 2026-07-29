"""Tests for protected routes, JWT auth, and refresh token flow."""

import pytest
from httpx import AsyncClient


async def _register_and_login(client: AsyncClient, payload: dict) -> dict:
    await client.post("/api/v1/auth/register", json=payload)
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": payload["email"], "password": payload["password"]},
    )
    return response.json()["data"]


@pytest.mark.asyncio
async def test_get_me_with_valid_token_succeeds(client: AsyncClient, valid_user_payload: dict):
    tokens = await _register_and_login(client, valid_user_payload)

    response = await client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["email"] == valid_user_payload["email"]


@pytest.mark.asyncio
async def test_get_me_without_token_returns_401(client: AsyncClient):
    response = await client.get("/api/v1/users/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_me_with_invalid_token_returns_401(client: AsyncClient):
    response = await client.get(
        "/api/v1/users/me",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_admin_route_forbidden_for_regular_user(client: AsyncClient, valid_user_payload: dict):
    tokens = await _register_and_login(client, valid_user_payload)

    # Any valid user ID; a regular user should be forbidden regardless.
    me = await client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    user_id = me.json()["data"]["id"]

    response = await client.get(
        f"/api/v1/users/{user_id}",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_refresh_token_rotation_issues_new_pair(client: AsyncClient, valid_user_payload: dict):
    tokens = await _register_and_login(client, valid_user_payload)

    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )

    assert response.status_code == 200
    new_tokens = response.json()["data"]
    assert new_tokens["access_token"] != tokens["access_token"]
    assert new_tokens["refresh_token"] != tokens["refresh_token"]


@pytest.mark.asyncio
async def test_reusing_rotated_refresh_token_fails(client: AsyncClient, valid_user_payload: dict):
    tokens = await _register_and_login(client, valid_user_payload)

    # First refresh rotates and revokes the original token.
    await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    # Replaying the now-revoked original refresh token must fail.
    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_refresh_token(client: AsyncClient, valid_user_payload: dict):
    tokens = await _register_and_login(client, valid_user_payload)

    logout_response = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert logout_response.status_code == 200

    refresh_response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refresh_response.status_code == 401
