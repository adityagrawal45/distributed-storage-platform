"""
Phase 10 security tests.

Scope: the genuine gaps found and fixed by the Phase 10 security audit
(see docs/security/) — audit-trail writes, refresh-token reuse ->
full-session-family revocation, and /auth/refresh being rate-limited.
IDOR / cross-user isolation is deliberately NOT re-tested exhaustively
here: `tests/test_file_storage.py` and `tests/test_folders.py` already
assert ownership checks (`get_active_by_id(id, owner_id)`-style repo
queries) reject a non-owner across the existing upload/download/
delete/folder surface — duplicating that here would test the same
code path a second time under a different filename. This file adds
two additional cross-user checks specifically for the two Phase-10-
audited operations (permanent delete, signed URL) so the audit trail
itself is exercised on a genuine authorization boundary, not only on
the happy path.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config.settings import Settings
from app.core.enums import AuditEventType
from app.core.rate_limiter import RateLimiter
from app.core.cache.keys import CacheKeyBuilder
from app.dependencies.rate_limit import get_rate_limiter
from app.main import app
from app.models.audit_log import AuditLog
from app.models.user import User, UserRole
from tests.fakes.fake_redis import FakeRedisClient


async def _register_and_login(client: AsyncClient, payload: dict) -> dict:
    await client.post("/api/v1/auth/register", json=payload)
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": payload["email"], "password": payload["password"]},
    )
    tokens = response.json()["data"]
    # Unlike test_protected_routes.py (which passes an explicit
    # `headers=` per request), this file's tests juggle multiple users
    # within one test to exercise cross-user isolation, so the client's
    # default header is set here and re-set explicitly wherever a test
    # switches identity — matching the `authed_client` fixture's own
    # behavior in tests/conftest.py.
    client.headers["Authorization"] = f"Bearer {tokens['access_token']}"
    return tokens


async def _audit_rows(db_session: AsyncSession, event_type: AuditEventType) -> list[AuditLog]:
    result = await db_session.execute(
        select(AuditLog).where(AuditLog.event_type == event_type).order_by(AuditLog.created_at)
    )
    return list(result.scalars().all())


async def _upload_one_file(client: AsyncClient) -> str:
    response = await client.post(
        "/api/v1/files/upload",
        files={"file": ("secret.txt", b"top secret contents", "text/plain")},
    )
    assert response.status_code == 201
    return response.json()["data"]["file"]["id"]


# =====================================================================
# Audit trail — login / logout / refresh
# =====================================================================


@pytest.mark.asyncio
async def test_login_success_writes_an_audit_row(
    client: AsyncClient, db_session: AsyncSession, valid_user_payload: dict
):
    await client.post("/api/v1/auth/register", json=valid_user_payload)
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": valid_user_payload["email"], "password": valid_user_payload["password"]},
    )
    assert response.status_code == 200

    rows = await _audit_rows(db_session, AuditEventType.LOGIN_SUCCESS)
    assert len(rows) == 1
    assert rows[0].result.value == "success"
    assert rows[0].actor_email == valid_user_payload["email"]
    assert rows[0].actor_user_id is not None


@pytest.mark.asyncio
async def test_login_failure_wrong_password_writes_an_audit_row_with_no_secret_leaked(
    client: AsyncClient, db_session: AsyncSession, valid_user_payload: dict
):
    await client.post("/api/v1/auth/register", json=valid_user_payload)
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": valid_user_payload["email"], "password": "TotallyWrong1!"},
    )
    assert response.status_code == 401

    rows = await _audit_rows(db_session, AuditEventType.LOGIN_FAILURE)
    assert len(rows) == 1
    assert rows[0].result.value == "failure"
    assert rows[0].actor_email == valid_user_payload["email"]
    # The failed password must never appear anywhere in the row.
    for value in (rows[0].actor_email, rows[0].detail):
        assert value is None or "TotallyWrong1!" not in str(value)


@pytest.mark.asyncio
async def test_login_failure_unknown_email_still_writes_an_audit_row(
    client: AsyncClient, db_session: AsyncSession
):
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": "nobody@nimbusfs.io", "password": "WhateverP@ss1"},
    )
    assert response.status_code == 401

    rows = await _audit_rows(db_session, AuditEventType.LOGIN_FAILURE)
    assert len(rows) == 1
    # No user exists for this email — actor_user_id must be null, not fabricated.
    assert rows[0].actor_user_id is None
    assert rows[0].actor_email == "nobody@nimbusfs.io"


@pytest.mark.asyncio
async def test_logout_writes_an_audit_row(
    client: AsyncClient, db_session: AsyncSession, valid_user_payload: dict
):
    tokens = await _register_and_login(client, valid_user_payload)
    response = await client.post("/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 200

    rows = await _audit_rows(db_session, AuditEventType.LOGOUT)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_successful_refresh_writes_a_token_refresh_audit_row(
    client: AsyncClient, db_session: AsyncSession, valid_user_payload: dict
):
    tokens = await _register_and_login(client, valid_user_payload)
    response = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 200

    rows = await _audit_rows(db_session, AuditEventType.TOKEN_REFRESH)
    assert len(rows) == 1


# =====================================================================
# Refresh-token reuse -> whole-session-family revocation (the Phase 10
# hardening — previously, replaying a revoked token was only rejected,
# not reacted to).
# =====================================================================


@pytest.mark.asyncio
async def test_replaying_a_rotated_refresh_token_revokes_every_other_live_session(
    client: AsyncClient, db_session: AsyncSession, valid_user_payload: dict
):
    # Session A: register + login.
    session_a = await _register_and_login(client, valid_user_payload)

    # Session A rotates once (tokens_a1 -> tokens_a2); the original
    # session_a refresh token is now revoked in the DB.
    rotated = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": session_a["refresh_token"]}
    )
    assert rotated.status_code == 200
    session_a2 = rotated.json()["data"]

    # A second, independent session B for the SAME user (e.g. a second
    # device), unrelated to session A's rotation.
    login_b = await client.post(
        "/api/v1/auth/login",
        data={"username": valid_user_payload["email"], "password": valid_user_payload["password"]},
    )
    session_b = login_b.json()["data"]

    # An attacker replays the ORIGINAL (now-revoked) session_a refresh
    # token — e.g. it was stolen before rotation happened.
    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": session_a["refresh_token"]}
    )
    assert replay.status_code == 401

    # Both the legitimate rotated session (A2) and the unrelated
    # session (B) must now ALSO be dead — the whole family was revoked,
    # not just the replayed token.
    refresh_a2 = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": session_a2["refresh_token"]}
    )
    assert refresh_a2.status_code == 401

    refresh_b = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": session_b["refresh_token"]}
    )
    assert refresh_b.status_code == 401

    rows = await _audit_rows(db_session, AuditEventType.TOKEN_REVOCATION)
    # Three rows, not one: the reuse-detection reaction fires on EVERY
    # presentation of an already-revoked token, and this test itself
    # goes on to replay two MORE now-revoked tokens (refresh_a2,
    # refresh_b) after the family-wide revoke — each of those is
    # ALSO, correctly, a replay of a revoked token and gets its own
    # audit row. This is more defensive than "only the first replay is
    # noticed," not a bug: a real attacker retrying a burned token
    # repeatedly should generate a repeated, reviewable signal, not go
    # quiet after the first attempt.
    assert len(rows) == 3
    assert all(row.result.value == "failure" for row in rows)
    assert all(row.detail["reason"] == "refresh_token_reuse_detected" for row in rows)
    # The FIRST replay is the one that actually revoked the other two
    # live sessions (A2's and B's tokens); the two replays that follow
    # (of tokens already revoked by that first reaction) have nothing
    # left to revoke.
    assert rows[0].detail["sessions_revoked"] == 2
    assert rows[1].detail["sessions_revoked"] == 0
    assert rows[2].detail["sessions_revoked"] == 0


# =====================================================================
# /auth/refresh rate limiting (previously unmetered — a real Phase 10
# finding, not a duplicate of test_rate_limiting.py's login/register
# coverage).
# =====================================================================


@pytest.fixture
def tight_refresh_limit(fake_redis_client: FakeRedisClient):
    settings = Settings(
        RATE_LIMIT_LOGIN_REQUESTS=1000,
        RATE_LIMIT_REGISTER_REQUESTS=1000,
        RATE_LIMIT_REFRESH_REQUESTS=2,
        RATE_LIMIT_REFRESH_WINDOW_SECONDS=60,
    )
    limiter = RateLimiter(fake_redis_client, settings, CacheKeyBuilder(settings.CACHE_KEY_PREFIX))
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    yield limiter
    app.dependency_overrides.pop(get_rate_limiter, None)


@pytest.mark.asyncio
async def test_refresh_endpoint_is_rate_limited(
    client: AsyncClient, valid_user_payload: dict, tight_refresh_limit
):
    tokens = await _register_and_login(client, valid_user_payload)

    # Two attempts (even with a token that's already been burned by
    # rotation, or malformed) still consume the REFRESH bucket, since
    # rate limiting runs as a route dependency BEFORE the handler body.
    first = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    second = await client.post("/api/v1/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert first.status_code != 429 and second.status_code != 429

    third = await client.post("/api/v1/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert third.status_code == 429
    assert third.headers["X-RateLimit-Category"] == "refresh"


# =====================================================================
# FILE_DELETE / FILE_DOWNLOAD audit trail + cross-user isolation on
# the two operations this phase actually audits.
# =====================================================================


@pytest.mark.asyncio
async def test_permanent_delete_writes_a_file_delete_audit_row(
    client: AsyncClient, db_session: AsyncSession, valid_user_payload: dict
):
    await _register_and_login(client, valid_user_payload)
    file_id = await _upload_one_file(client)

    trash = await client.delete(f"/api/v1/metadata/{file_id}")
    assert trash.status_code == 200

    delete = await client.delete(f"/api/v1/files/{file_id}/permanent")
    assert delete.status_code == 200

    rows = await _audit_rows(db_session, AuditEventType.FILE_DELETE)
    assert len(rows) == 1
    assert str(rows[0].resource_id) == file_id


@pytest.mark.asyncio
async def test_download_writes_a_file_download_audit_row(
    client: AsyncClient, db_session: AsyncSession, valid_user_payload: dict
):
    await _register_and_login(client, valid_user_payload)
    file_id = await _upload_one_file(client)

    download = await client.get(f"/api/v1/files/{file_id}/download")
    assert download.status_code == 200

    rows = await _audit_rows(db_session, AuditEventType.FILE_DOWNLOAD)
    assert len(rows) == 1
    assert rows[0].detail["method"] == "direct"


@pytest.mark.asyncio
async def test_signed_url_writes_a_file_download_audit_row_without_logging_the_url(
    client: AsyncClient, db_session: AsyncSession, valid_user_payload: dict
):
    await _register_and_login(client, valid_user_payload)
    file_id = await _upload_one_file(client)

    signed = await client.get(f"/api/v1/files/{file_id}/signed-url")
    assert signed.status_code == 200
    issued_url = signed.json()["data"]["url"]

    rows = await _audit_rows(db_session, AuditEventType.FILE_DOWNLOAD)
    assert len(rows) == 1
    assert rows[0].detail["method"] == "signed_url"
    # The bearer-credential URL itself must never be persisted in the
    # audit trail — only the fact that one was issued.
    assert issued_url not in str(rows[0].detail)


@pytest.mark.asyncio
async def test_a_user_cannot_get_a_signed_url_for_another_users_file(client: AsyncClient):
    owner_payload = {
        "first_name": "Owner",
        "last_name": "One",
        "email": "owner@nimbusfs.io",
        "password": "StrongP@ssw0rd",
    }
    attacker_payload = {
        "first_name": "Attacker",
        "last_name": "Two",
        "email": "attacker@nimbusfs.io",
        "password": "StrongP@ssw0rd",
    }

    owner_tokens = await _register_and_login(client, owner_payload)
    client.headers["Authorization"] = f"Bearer {owner_tokens['access_token']}"
    file_id = await _upload_one_file(client)

    attacker_tokens = await _register_and_login(client, attacker_payload)
    client.headers["Authorization"] = f"Bearer {attacker_tokens['access_token']}"

    response = await client.get(f"/api/v1/files/{file_id}/signed-url")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_user_cannot_permanently_delete_another_users_file(client: AsyncClient):
    owner_payload = {
        "first_name": "Owner",
        "last_name": "One",
        "email": "owner2@nimbusfs.io",
        "password": "StrongP@ssw0rd",
    }
    attacker_payload = {
        "first_name": "Attacker",
        "last_name": "Two",
        "email": "attacker2@nimbusfs.io",
        "password": "StrongP@ssw0rd",
    }

    owner_tokens = await _register_and_login(client, owner_payload)
    client.headers["Authorization"] = f"Bearer {owner_tokens['access_token']}"
    file_id = await _upload_one_file(client)
    await client.delete(f"/api/v1/metadata/{file_id}")  # trash it as the owner

    attacker_tokens = await _register_and_login(client, attacker_payload)
    client.headers["Authorization"] = f"Bearer {attacker_tokens['access_token']}"

    response = await client.delete(f"/api/v1/files/{file_id}/permanent")
    assert response.status_code == 404


# =====================================================================
# ADMIN_ACTION audit trail (Phase 10). No API path exists to self-
# promote to ADMIN (by design — `AuthService.register` never accepts a
# client-supplied role), so the admin fixture here is created directly
# against the test database, exactly the kind of operational step a
# real deployment would do out-of-band (a DB migration/seed, or a
# future dedicated admin-provisioning tool — not an API endpoint).
# =====================================================================


@pytest.mark.asyncio
async def test_admin_viewing_another_users_profile_writes_an_admin_action_audit_row(
    client: AsyncClient, db_session: AsyncSession
):
    admin_payload = {
        "first_name": "Admin",
        "last_name": "User",
        "email": "admin@nimbusfs.io",
        "password": "StrongP@ssw0rd",
    }
    target_payload = {
        "first_name": "Target",
        "last_name": "User",
        "email": "target@nimbusfs.io",
        "password": "StrongP@ssw0rd",
    }

    admin_tokens = await _register_and_login(client, admin_payload)
    target_tokens = await _register_and_login(client, target_payload)

    # Promote to ADMIN directly in the DB — see the module-level
    # docstring above this test for why no API path does this.
    # `require_role` checks `current_user.role`, and `get_current_user`
    # re-fetches the User row from the DB on every request (the same
    # "authorization state is never trusted from the JWT claim alone"
    # policy that already makes `is_active` take effect immediately) —
    # so this takes effect without needing a fresh token.
    admin_row = (
        await db_session.execute(select(User).where(User.email == admin_payload["email"]))
    ).scalar_one()
    admin_row.role = UserRole.ADMIN
    await db_session.flush()

    client.headers["Authorization"] = f"Bearer {target_tokens['access_token']}"
    target_id = (await client.get("/api/v1/users/me")).json()["data"]["id"]

    client.headers["Authorization"] = f"Bearer {admin_tokens['access_token']}"
    admin_id = (await client.get("/api/v1/users/me")).json()["data"]["id"]

    response = await client.get(f"/api/v1/users/{target_id}")
    assert response.status_code == 200

    rows = await _audit_rows(db_session, AuditEventType.ADMIN_ACTION)
    assert len(rows) == 1
    assert str(rows[0].actor_user_id) == admin_id
    assert str(rows[0].resource_id) == target_id
