"""
Tests for Phase 6: chunked / resumable large-file uploads.

Runs entirely against the hermetic fakes the rest of the suite uses
(SQLite, `FakeGCSClient`, `FakeRedisClient`) — no real GCS/Redis/Postgres
needed, matching the project-wide "no external services to run pytest"
design (see CONTEXT.md).
"""

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tests.fakes.fake_gcs import FakeBlob, FakeGCSClient
from tests.fakes.fake_redis import FakeRedisClient

CHUNK_SIZE = 1024  # small, deliberately not aligned to any GCS constraint — see StorageService.compose_objects docstring


def _chunk_bytes(chunk_number: int, size: int = CHUNK_SIZE) -> bytes:
    """Deterministic, chunk-number-dependent content so reassembly can be verified byte-exactly."""
    return bytes([chunk_number % 256]) * size


async def _initiate(
    client: AsyncClient,
    *,
    filename: str = "movie.mp4",
    total_chunks: int = 3,
    last_chunk_size: int | None = None,
    chunk_size: int = CHUNK_SIZE,
    mime_type: str = "video/mp4",
    checksum: str | None = None,
    folder_id: str | None = None,
    headers: dict | None = None,
) -> dict:
    last_size = last_chunk_size if last_chunk_size is not None else chunk_size
    total_size = chunk_size * (total_chunks - 1) + last_size
    payload = {"filename": filename, "size": total_size, "mime_type": mime_type, "chunk_size": chunk_size}
    if checksum:
        payload["checksum"] = checksum
    if folder_id:
        payload["folder_id"] = folder_id
    response = await client.post("/api/v1/uploads", json=payload, headers=headers or {})
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _upload_all_chunks(client: AsyncClient, upload_id: str, total_chunks: int, last_chunk_size: int) -> bytes:
    """Uploads every chunk (in order, for simplicity) and returns the expected reassembled content."""
    full_content = b""
    for n in range(1, total_chunks + 1):
        size = CHUNK_SIZE if n < total_chunks else last_chunk_size
        data = _chunk_bytes(n, size)
        full_content += data
        response = await client.put(f"/api/v1/uploads/{upload_id}/chunks/{n}", content=data)
        assert response.status_code == 200, response.text
    return full_content


# ---------------------------------------------------------------------
# 1. Initiate upload
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_initiate_upload_creates_session(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=5, last_chunk_size=500)
    assert data["status"] == "initiated"
    assert data["total_chunks"] == 5
    assert data["chunk_size"] == CHUNK_SIZE
    assert uuid.UUID(data["upload_id"])
    assert data["expires_at"]


@pytest.mark.asyncio
async def test_initiate_upload_rejects_folder_that_does_not_exist(authed_client: AsyncClient):
    response = await authed_client.post(
        "/api/v1/uploads",
        json={
            "filename": "a.bin",
            "size": CHUNK_SIZE,
            "chunk_size": CHUNK_SIZE,
            "folder_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------
# 2 & 3. Upload first chunk / multiple chunks
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_upload_first_chunk_transitions_session_to_uploading(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=3, last_chunk_size=200)
    upload_id = data["upload_id"]

    response = await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=_chunk_bytes(1))
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["chunk_number"] == 1
    assert body["status"] == "verified"

    status_response = await authed_client.get(f"/api/v1/uploads/{upload_id}")
    assert status_response.json()["data"]["status"] == "uploading"


@pytest.mark.asyncio
async def test_upload_multiple_chunks_updates_progress(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=4, last_chunk_size=300)
    upload_id = data["upload_id"]

    await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=_chunk_bytes(1))
    response = await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/2", content=_chunk_bytes(2))
    body = response.json()["data"]
    assert body["uploaded_bytes"] == CHUNK_SIZE * 2
    assert body["progress_percentage"] == round(CHUNK_SIZE * 2 / data["total_size"] * 100, 2)


# ---------------------------------------------------------------------
# 4. Parallel chunk uploads
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chunks_upload_correctly_when_sent_out_of_order(authed_client: AsyncClient):
    """
    Uploads chunks in reverse order (6, 5, 4, ..., 1) — not sequential
    — proving the endpoint has no ordering assumption, since GCS
    Compose at completion sorts by `chunk_number`, not arrival order.

    Deliberately sequential *requests* here, not `asyncio.gather`: this
    test suite's `db_session` fixture is ONE SQLAlchemy `AsyncSession`
    shared across every request in a test (see tests/conftest.py) —
    fine for the single-writer-at-a-time flows the rest of the suite
    exercises, but `AsyncSession` is explicitly not safe for concurrent
    use from multiple asyncio tasks. In production, every request gets
    its OWN session (`app/database/session.py::get_db`), so genuine
    task-level parallelism is safe there; the real concurrency
    guarantees (unique constraint, per-chunk Redis lock) are exercised
    directly against `FakeRedisClient`/the repository layer in the
    dedicated lock/repository tests instead of over this shared fixture.
    """
    data = await _initiate(authed_client, total_chunks=6, last_chunk_size=700)
    upload_id = data["upload_id"]

    for n in reversed(range(1, 7)):
        size = 700 if n == 6 else CHUNK_SIZE
        response = await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/{n}", content=_chunk_bytes(n, size))
        assert response.status_code == 200

    status_response = await authed_client.get(f"/api/v1/uploads/{upload_id}")
    body = status_response.json()["data"]
    assert sorted(body["uploaded_chunks"]) == list(range(1, 7))
    assert body["missing_chunks"] == []
    assert body["uploaded_bytes"] == data["total_size"]


@pytest.mark.asyncio
async def test_chunk_upload_endpoint_handles_overlapping_requests_without_corruption(authed_client: AsyncClient):
    """
    A lighter-weight concurrency smoke test than the one above: two
    DIFFERENT chunk numbers fired via `asyncio.gather`. This still
    shares one `AsyncSession` (see the docstring above), so it isn't
    proof of production-grade parallelism, but it does confirm the
    endpoint doesn't hard-crash or corrupt state under a small amount
    of overlap — accepting either outcome is "fine" is the same
    tolerant-of-interleaving pattern Phase 4's concurrent-idempotency
    tests use.
    """
    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]

    responses = await asyncio.gather(
        authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=_chunk_bytes(1)),
        authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/2", content=_chunk_bytes(2, 400)),
        return_exceptions=True,
    )
    ok = [r for r in responses if not isinstance(r, Exception) and r.status_code == 200]
    assert len(ok) >= 1  # at least one must succeed cleanly; neither may silently corrupt the other's row

    status_response = await authed_client.get(f"/api/v1/uploads/{upload_id}")
    uploaded = status_response.json()["data"]["uploaded_chunks"]
    assert len(uploaded) == len(set(uploaded))  # never duplicated


# ---------------------------------------------------------------------
# 5. Duplicate chunk (safe retry vs. real conflict)
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reuploading_identical_chunk_is_a_safe_noop(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]
    chunk_data = _chunk_bytes(1)

    first = await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=chunk_data)
    second = await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=chunk_data)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"]["checksum"] == second.json()["data"]["checksum"]

    status_response = await authed_client.get(f"/api/v1/uploads/{upload_id}")
    assert status_response.json()["data"]["uploaded_chunks"] == [1]  # not duplicated


@pytest.mark.asyncio
async def test_reuploading_chunk_with_different_content_overwrites_it(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]

    await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=_chunk_bytes(1))
    response = await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=_chunk_bytes(99))
    assert response.status_code == 200
    assert response.json()["data"]["checksum"] == hashlib.sha256(_chunk_bytes(99)).hexdigest()


# ---------------------------------------------------------------------
# 6. Invalid chunk
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chunk_with_wrong_size_is_rejected(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=3, last_chunk_size=200)
    upload_id = data["upload_id"]
    response = await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=b"too-short")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chunk_number_out_of_range_is_rejected(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=3, last_chunk_size=200)
    upload_id = data["upload_id"]
    response = await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/99", content=_chunk_bytes(99))
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chunk_exceeding_configured_max_size_is_rejected(authed_client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    from app.core.config import get_settings

    # Initiate FIRST, at a valid chunk_size, then shrink the server's
    # bounded-read ceiling below that — isolates
    # `_read_body_bounded`'s own defensive cap (independent of a
    # session's configured chunk_size) rather than re-testing the
    # initiate-time chunk_size range validation.
    data = await _initiate(authed_client, total_chunks=1, last_chunk_size=CHUNK_SIZE)
    upload_id = data["upload_id"]

    monkeypatch.setattr(get_settings(), "CHUNK_MAX_SIZE_BYTES", 100)
    response = await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=_chunk_bytes(1, CHUNK_SIZE))
    assert response.status_code == 400


# ---------------------------------------------------------------------
# 13. Checksum mismatch
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chunk_checksum_mismatch_is_rejected(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]
    response = await authed_client.put(
        f"/api/v1/uploads/{upload_id}/chunks/1",
        content=_chunk_bytes(1),
        headers={"X-Chunk-Checksum": "0" * 64},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_correct_chunk_checksum_is_accepted(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]
    chunk_data = _chunk_bytes(1)
    response = await authed_client.put(
        f"/api/v1/uploads/{upload_id}/chunks/1",
        content=chunk_data,
        headers={"X-Chunk-Checksum": hashlib.sha256(chunk_data).hexdigest()},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_final_checksum_mismatch_fails_completion(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400, checksum="0" * 64)
    upload_id = data["upload_id"]
    await _upload_all_chunks(authed_client, upload_id, 2, 400)

    response = await authed_client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert response.status_code == 400

    status_response = await authed_client.get(f"/api/v1/uploads/{upload_id}")
    assert status_response.json()["data"]["status"] == "failed"


# ---------------------------------------------------------------------
# 7. Missing chunk (completion rejected)
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_completion_rejected_when_chunks_are_missing(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=3, last_chunk_size=200)
    upload_id = data["upload_id"]
    await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=_chunk_bytes(1))
    # chunk 2 and 3 never uploaded

    response = await authed_client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert response.status_code == 400
    assert "missing" in response.json()["message"].lower()


# ---------------------------------------------------------------------
# 8. Resume upload
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resume_upload_reports_correct_missing_chunks_and_completes(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=5, last_chunk_size=250)
    upload_id = data["upload_id"]

    for n in (1, 2, 4):
        await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/{n}", content=_chunk_bytes(n))

    status_response = await authed_client.get(f"/api/v1/uploads/{upload_id}")
    body = status_response.json()["data"]
    assert sorted(body["uploaded_chunks"]) == [1, 2, 4]
    assert sorted(body["missing_chunks"]) == [3, 5]

    # "Resume": upload exactly the reported missing chunks, nothing else.
    await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/3", content=_chunk_bytes(3))
    await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/5", content=_chunk_bytes(5, 250))

    complete_response = await authed_client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert complete_response.status_code == 200, complete_response.text


# ---------------------------------------------------------------------
# 9. Upload expiration
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_expired_upload_session_rejects_new_chunks(authed_client: AsyncClient, db_session):
    from app.models.upload_session import UploadSession

    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = uuid.UUID(data["upload_id"])

    result = await db_session.execute(select(UploadSession).where(UploadSession.id == upload_id))
    session = result.scalar_one()
    session.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.flush()

    response = await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=_chunk_bytes(1))
    assert response.status_code == 409

    status_response = await authed_client.get(f"/api/v1/uploads/{upload_id}")
    assert status_response.json()["data"]["status"] == "expired"


# ---------------------------------------------------------------------
# 10. Upload cancellation
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_upload_deletes_temp_objects_and_is_idempotent(
    authed_client: AsyncClient, fake_gcs_client: FakeGCSClient
):
    from app.core.config import get_settings

    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]
    await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=_chunk_bytes(1))

    bucket = fake_gcs_client.bucket(get_settings().GCS_BUCKET_NAME)
    assert len(bucket.store) == 1  # the one temp chunk object

    first = await authed_client.post(f"/api/v1/uploads/{upload_id}/cancel")
    assert first.status_code == 200
    assert first.json()["data"]["status"] == "cancelled"
    assert len(bucket.store) == 0  # temp object cleaned up

    second = await authed_client.post(f"/api/v1/uploads/{upload_id}/cancel")
    assert second.status_code == 200  # idempotent, not an error
    assert second.json()["data"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cannot_cancel_completed_upload(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=1, last_chunk_size=CHUNK_SIZE)
    upload_id = data["upload_id"]
    await _upload_all_chunks(authed_client, upload_id, 1, CHUNK_SIZE)
    await authed_client.post(f"/api/v1/uploads/{upload_id}/complete")

    response = await authed_client.post(f"/api/v1/uploads/{upload_id}/cancel")
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_cannot_upload_chunks_after_cancellation(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]
    await authed_client.post(f"/api/v1/uploads/{upload_id}/cancel")

    response = await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=_chunk_bytes(1))
    assert response.status_code == 409


# ---------------------------------------------------------------------
# 11. Completion (end to end, byte-exact reassembly)
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_complete_upload_reassembles_bytes_exactly_and_is_downloadable(authed_client: AsyncClient):
    data = await _initiate(authed_client, filename="report.bin", total_chunks=5, last_chunk_size=333, mime_type="application/octet-stream")
    upload_id = data["upload_id"]
    expected_content = await _upload_all_chunks(authed_client, upload_id, 5, 333)

    response = await authed_client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "completed"
    assert body["actual_checksum"] == hashlib.sha256(expected_content).hexdigest()
    file_id = body["file"]["id"]
    assert body["file"]["size"] == len(expected_content)

    download = await authed_client.get(f"/api/v1/files/{file_id}/download")
    assert download.status_code == 200
    assert download.content == expected_content


@pytest.mark.asyncio
async def test_complete_upload_cleans_up_temp_chunk_objects(authed_client: AsyncClient, fake_gcs_client: FakeGCSClient):
    from app.core.config import get_settings

    data = await _initiate(authed_client, total_chunks=3, last_chunk_size=200)
    upload_id = data["upload_id"]
    await _upload_all_chunks(authed_client, upload_id, 3, 200)

    await authed_client.post(f"/api/v1/uploads/{upload_id}/complete")

    bucket = fake_gcs_client.bucket(get_settings().GCS_BUCKET_NAME)
    # Exactly one object should remain: the final composed file. All
    # per-chunk temp objects must have been deleted after compose.
    assert len(bucket.store) == 1


# ---------------------------------------------------------------------
# 12. Duplicate completion request
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_duplicate_completion_request_returns_same_result(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]
    await _upload_all_chunks(authed_client, upload_id, 2, 400)

    first = await authed_client.post(f"/api/v1/uploads/{upload_id}/complete")
    second = await authed_client.post(f"/api/v1/uploads/{upload_id}/complete")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"]["file"]["id"] == second.json()["data"]["file"]["id"]


@pytest.mark.asyncio
async def test_completion_with_idempotency_key_replays_response(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]
    await _upload_all_chunks(authed_client, upload_id, 2, 400)

    key = str(uuid.uuid4())
    first = await authed_client.post(f"/api/v1/uploads/{upload_id}/complete", headers={"Idempotency-Key": key})
    second = await authed_client.post(f"/api/v1/uploads/{upload_id}/complete", headers={"Idempotency-Key": key})
    assert first.json()["data"]["file"]["id"] == second.json()["data"]["file"]["id"]


# ---------------------------------------------------------------------
# 20. Concurrent completion
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_completion_requests_never_create_two_files(authed_client: AsyncClient):
    data = await _initiate(authed_client, filename="race.bin", total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]
    await _upload_all_chunks(authed_client, upload_id, 2, 400)

    responses = await asyncio.gather(
        authed_client.post(f"/api/v1/uploads/{upload_id}/complete"),
        authed_client.post(f"/api/v1/uploads/{upload_id}/complete"),
        return_exceptions=True,
    )
    ok_responses = [r for r in responses if not isinstance(r, Exception) and r.status_code == 200]
    assert len(ok_responses) >= 1

    search = await authed_client.get("/api/v1/metadata/search", params={"q": "race.bin"})
    assert search.json()["data"]["total"] == 1


# ---------------------------------------------------------------------
# 14 & 15. Unauthorized access / upload ownership
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_upload_endpoints_require_authentication(client: AsyncClient):
    response = await client.post("/api/v1/uploads", json={"filename": "a.bin", "size": 100, "chunk_size": 100})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_upload_session_is_scoped_to_owner(client: AsyncClient):
    user_a = {"first_name": "A", "last_name": "One", "email": "a@nimbusfs.io", "password": "StrongP@ss1!"}
    user_b = {"first_name": "B", "last_name": "Two", "email": "b@nimbusfs.io", "password": "StrongP@ss2!"}

    await client.post("/api/v1/auth/register", json=user_a)
    login_a = await client.post("/api/v1/auth/login", data={"username": user_a["email"], "password": user_a["password"]})
    token_a = login_a.json()["data"]["access_token"]

    await client.post("/api/v1/auth/register", json=user_b)
    login_b = await client.post("/api/v1/auth/login", data={"username": user_b["email"], "password": user_b["password"]})
    token_b = login_b.json()["data"]["access_token"]

    client.headers["Authorization"] = f"Bearer {token_a}"
    data = await _initiate(client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]

    client.headers["Authorization"] = f"Bearer {token_b}"
    response = await client.get(f"/api/v1/uploads/{upload_id}")
    assert response.status_code == 404  # never leaks "exists but not yours" vs "doesn't exist"


@pytest.mark.asyncio
async def test_nonexistent_upload_session_returns_404(authed_client: AsyncClient):
    response = await authed_client.get(f"/api/v1/uploads/{uuid.uuid4()}")
    assert response.status_code == 404


# ---------------------------------------------------------------------
# 16. Invalid state transition
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_completing_a_cancelled_upload_is_rejected(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]
    await authed_client.post(f"/api/v1/uploads/{upload_id}/cancel")

    response = await authed_client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert response.status_code in (400, 409)


# ---------------------------------------------------------------------
# 17. Database failure
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_database_failure_returns_503(authed_client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    from sqlalchemy.exc import OperationalError

    from app.repositories.upload_session_repository import UploadSessionRepository

    async def _boom(self, upload_id, owner_id):
        raise OperationalError("statement", {}, Exception("connection lost"))

    monkeypatch.setattr(UploadSessionRepository, "get_owned", _boom)

    response = await authed_client.get(f"/api/v1/uploads/{uuid.uuid4()}")
    assert response.status_code == 503


# ---------------------------------------------------------------------
# 18. GCS failure
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gcs_failure_during_chunk_upload_returns_502(authed_client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    from google.api_core import exceptions as gcs_exceptions

    def _boom(self, stream, content_type=None, size=None):
        raise gcs_exceptions.ServiceUnavailable("simulated outage")

    monkeypatch.setattr(FakeBlob, "upload_from_file", _boom)

    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]
    response = await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=_chunk_bytes(1))
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_gcs_failure_during_compose_marks_session_failed(authed_client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    from google.api_core import exceptions as gcs_exceptions

    def _boom(self, sources, client=None):
        raise gcs_exceptions.ServiceUnavailable("simulated compose outage")

    monkeypatch.setattr(FakeBlob, "compose", _boom)

    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]
    await _upload_all_chunks(authed_client, upload_id, 2, 400)

    response = await authed_client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert response.status_code >= 500 or response.status_code == 502

    status_response = await authed_client.get(f"/api/v1/uploads/{upload_id}")
    assert status_response.json()["data"]["status"] == "failed"


# ---------------------------------------------------------------------
# 19. Redis failure
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_redis_failure_during_chunk_upload_fails_safely(authed_client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    # RuntimeError, not ConnectionError/OSError — the latter is treated
    # as a real transport-level disconnect by the ASGI test transport
    # (anyio's stream machinery), not a generic application exception,
    # so it doesn't reach `unhandled_exception_handler` the same way a
    # real `redis.exceptions.ConnectionError` reaching the same code
    # path in production would (production goes through a real socket,
    # not the in-process ASGI transport this test uses).
    async def _boom(self, name, value, nx=False, ex=None, px=None):
        raise RuntimeError("simulated redis outage")

    monkeypatch.setattr(FakeRedisClient, "set", _boom)

    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]
    response = await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=_chunk_bytes(1))
    # Fails closed (never silently skips the coordination lock and
    # risks a lost-update race) — a 5xx, not a corrupted 200.
    assert response.status_code >= 500

    status_response = await authed_client.get(f"/api/v1/uploads/{upload_id}")
    assert status_response.json()["data"]["uploaded_chunks"] == []  # no partial/corrupt state persisted


# ---------------------------------------------------------------------
# 21. Large file metadata
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_initiate_upload_handles_large_declared_size_correctly(authed_client: AsyncClient):
    ten_gb = 10 * 1024 * 1024 * 1024
    chunk_size = 64 * 1024 * 1024  # 64 MiB
    response = await authed_client.post(
        "/api/v1/uploads",
        json={"filename": "huge.iso", "size": ten_gb, "mime_type": "application/octet-stream", "chunk_size": chunk_size},
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["total_size"] == ten_gb
    assert data["total_chunks"] == -(-ten_gb // chunk_size)  # ceiling division


# ---------------------------------------------------------------------
# 22. Invalid file size
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_zero_size_upload_is_rejected(authed_client: AsyncClient):
    response = await authed_client.post(
        "/api/v1/uploads", json={"filename": "empty.bin", "size": 0, "chunk_size": CHUNK_SIZE}
    )
    assert response.status_code in (400, 422)


@pytest.mark.asyncio
async def test_oversized_upload_is_rejected(authed_client: AsyncClient):
    too_large = 200 * 1024 * 1024 * 1024 * 1024  # 200 TB, past MAX_CHUNKED_UPLOAD_SIZE_GB default (100 GB)
    response = await authed_client.post(
        "/api/v1/uploads",
        json={"filename": "too-big.bin", "size": too_large, "chunk_size": 64 * 1024 * 1024},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chunk_size_producing_too_many_chunks_is_rejected(authed_client: AsyncClient):
    from app.core.config import get_settings

    settings = get_settings()
    # Smallest allowed chunk_size, with a total_size just large enough
    # to require more than MAX_CHUNKS_PER_UPLOAD chunks at that size —
    # still comfortably under MAX_CHUNKED_UPLOAD_SIZE_BYTES, so this
    # isolates the chunk-COUNT check specifically, not the size cap.
    min_chunk = settings.CHUNK_MIN_SIZE_BYTES
    too_many_chunks_size = min_chunk * (settings.MAX_CHUNKS_PER_UPLOAD + 10)
    response = await authed_client.post(
        "/api/v1/uploads",
        json={"filename": "many-chunks.bin", "size": too_many_chunks_size, "chunk_size": min_chunk},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------
# Delete endpoint
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_delete_active_upload_cancels_and_removes_it(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=2, last_chunk_size=400)
    upload_id = data["upload_id"]
    await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=_chunk_bytes(1))

    response = await authed_client.delete(f"/api/v1/uploads/{upload_id}")
    assert response.status_code == 200

    follow_up = await authed_client.get(f"/api/v1/uploads/{upload_id}")
    assert follow_up.status_code == 404


@pytest.mark.asyncio
async def test_delete_completed_upload_is_rejected(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=1, last_chunk_size=CHUNK_SIZE)
    upload_id = data["upload_id"]
    await _upload_all_chunks(authed_client, upload_id, 1, CHUNK_SIZE)
    await authed_client.post(f"/api/v1/uploads/{upload_id}/complete")

    response = await authed_client.delete(f"/api/v1/uploads/{upload_id}")
    assert response.status_code == 409


# ---------------------------------------------------------------------
# Chunk listing
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_chunks_returns_uploaded_chunk_records(authed_client: AsyncClient):
    data = await _initiate(authed_client, total_chunks=3, last_chunk_size=200)
    upload_id = data["upload_id"]
    await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/1", content=_chunk_bytes(1))
    await authed_client.put(f"/api/v1/uploads/{upload_id}/chunks/3", content=_chunk_bytes(3, 200))

    response = await authed_client.get(f"/api/v1/uploads/{upload_id}/chunks")
    assert response.status_code == 200
    chunks = response.json()["data"]
    assert sorted(c["chunk_number"] for c in chunks) == [1, 3]


# ---------------------------------------------------------------------
# Idempotency-Key on initiate
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_initiate_with_idempotency_key_replays_same_session(authed_client: AsyncClient):
    key = str(uuid.uuid4())
    first = await _initiate(authed_client, total_chunks=2, last_chunk_size=400, headers={"Idempotency-Key": key})
    second = await _initiate(authed_client, total_chunks=2, last_chunk_size=400, headers={"Idempotency-Key": key})
    assert first["upload_id"] == second["upload_id"]
