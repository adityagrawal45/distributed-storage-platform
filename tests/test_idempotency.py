"""
Idempotency-Key middleware (Phase 4).

Covers the two headline scenarios from README: preventing a duplicate
folder from a retried `POST /folders`, and preventing a duplicate file
from a retried `POST /files/upload` — plus the "already in flight"
conflict path and the case-insensitive header handling.
"""

import io
import uuid

import pytest
from httpx import AsyncClient

from app.middleware.idempotency import _fingerprint


class _FakeURL:
    def __init__(self, path: str):
        self.path = path


class _FakeRequest:
    """Minimal stand-in so tests can precompute the same fingerprint `_fingerprint()` would."""

    def __init__(self, headers: dict, method: str, path: str):
        self.headers = headers
        self.method = method
        self.url = _FakeURL(path)


@pytest.mark.asyncio
async def test_idempotency_key_replays_cached_response_for_folder_create(authed_client: AsyncClient):
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}
    payload = {"name": "Idempotent Folder"}

    first = await authed_client.post("/api/v1/folders", json=payload, headers=headers)
    assert first.status_code == 201
    assert "X-Idempotent-Replay" not in first.headers

    second = await authed_client.post("/api/v1/folders", json=payload, headers=headers)
    assert second.status_code == 201
    assert second.headers.get("X-Idempotent-Replay") == "true"
    assert second.json()["data"]["id"] == first.json()["data"]["id"]

    listing = await authed_client.get("/api/v1/folders")
    names = [f["name"] for f in listing.json()["data"]]
    assert names.count("Idempotent Folder") == 1


@pytest.mark.asyncio
async def test_without_idempotency_key_duplicate_post_hits_normal_conflict_rules(authed_client: AsyncClient):
    """No header -> no replay protection; the existing business-rule 409 (duplicate name) is what fires instead."""
    payload = {"name": "No Key Folder"}

    first = await authed_client.post("/api/v1/folders", json=payload)
    assert first.status_code == 201

    second = await authed_client.post("/api/v1/folders", json=payload)
    assert second.status_code == 409
    assert "X-Idempotent-Replay" not in second.headers


@pytest.mark.asyncio
async def test_idempotency_key_prevents_duplicate_upload(authed_client: AsyncClient):
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}
    file_content = b"idempotent upload payload"

    first = await authed_client.post(
        "/api/v1/files/upload",
        headers=headers,
        files={"file": ("report.pdf", io.BytesIO(file_content), "application/pdf")},
    )
    assert first.status_code == 201
    first_file_id = first.json()["data"]["file"]["id"]

    second = await authed_client.post(
        "/api/v1/files/upload",
        headers=headers,
        files={"file": ("report.pdf", io.BytesIO(file_content), "application/pdf")},
    )
    assert second.status_code == 201
    assert second.headers.get("X-Idempotent-Replay") == "true"
    assert second.json()["data"]["file"]["id"] == first_file_id

    search = await authed_client.get("/api/v1/metadata/search", params={"q": "report.pdf"})
    assert search.json()["data"]["total"] == 1


@pytest.mark.asyncio
async def test_idempotency_key_header_is_case_insensitive(authed_client: AsyncClient):
    key = str(uuid.uuid4())
    payload = {"name": "Case Insensitive Folder"}

    first = await authed_client.post("/api/v1/folders", json=payload, headers={"idempotency-key": key})
    second = await authed_client.post("/api/v1/folders", json=payload, headers={"IDEMPOTENCY-KEY": key})

    assert first.status_code == 201
    assert second.json()["data"]["id"] == first.json()["data"]["id"]


@pytest.mark.asyncio
async def test_concurrent_request_with_same_in_flight_key_is_rejected(
    authed_client: AsyncClient, fake_redis_client
):
    """
    Simulates a second retry arriving while the first attempt is still
    executing (the exact case idempotency exists to guard: two racing
    retries must not both create a resource) by pre-claiming the
    in-progress marker the middleware itself would claim.
    """
    key = str(uuid.uuid4())
    auth_header = authed_client.headers["Authorization"]
    fake_request = _FakeRequest(headers={"authorization": auth_header}, method="POST", path="/api/v1/folders")
    fingerprint = _fingerprint(fake_request, key)

    await fake_redis_client.set(f"idempotency:in-progress:{fingerprint}", "1", ex=60)

    response = await authed_client.post(
        "/api/v1/folders", json={"name": "Should Be Rejected"}, headers={"Idempotency-Key": key}
    )
    assert response.status_code == 409
    assert response.json()["success"] is False
