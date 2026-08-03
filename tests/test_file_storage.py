"""
Tests for Phase 3: cloud storage upload/download/replace/delete, signed
URLs, duplicate detection, validation, and rollback — all against the
in-memory `FakeGCSClient` (tests/fakes/fake_gcs.py), never real GCS.
"""

import io

import pytest
from httpx import AsyncClient

from app.dependencies.providers import get_gcs_client
from app.main import app
from tests.fakes.fake_gcs import FakeGCSClient

PDF_MAGIC_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"0" * 100
PNG_MAGIC_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 100


def _upload_payload(filename: str, content: bytes, content_type: str = "application/pdf") -> dict:
    return {"file": (filename, io.BytesIO(content), content_type)}


@pytest.mark.asyncio
async def test_upload_file_success(authed_client: AsyncClient):
    response = await authed_client.post("/api/v1/files/upload", files=_upload_payload("report.pdf", PDF_MAGIC_BYTES))
    assert response.status_code == 201
    body = response.json()["data"]
    assert body["is_duplicate"] is False
    file = body["file"]
    assert file["original_filename"] == "report.pdf"
    assert file["status"] == "active"
    assert file["upload_status"] == "completed"
    assert file["object_name"]
    assert file["bucket_name"]
    assert file["checksum"]
    assert file["size"] == len(PDF_MAGIC_BYTES)


@pytest.mark.asyncio
async def test_upload_into_nonexistent_folder_returns_404(authed_client: AsyncClient):
    response = await authed_client.post(
        "/api/v1/files/upload",
        data={"folder_id": "00000000-0000-0000-0000-000000000000"},
        files=_upload_payload("report.pdf", PDF_MAGIC_BYTES),
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_upload_duplicate_filename_same_folder_rejected(authed_client: AsyncClient):
    await authed_client.post("/api/v1/files/upload", files=_upload_payload("report.pdf", PDF_MAGIC_BYTES))
    response = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("report.pdf", PNG_MAGIC_BYTES)
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_upload_empty_file_rejected(authed_client: AsyncClient):
    response = await authed_client.post("/api/v1/files/upload", files=_upload_payload("empty.pdf", b""))
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_upload_blocked_extension_rejected(authed_client: AsyncClient):
    response = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("virus.exe", b"MZ" + b"0" * 100)
    )
    assert response.status_code == 415


@pytest.mark.asyncio
async def test_upload_duplicate_content_is_deduplicated(
    authed_client: AsyncClient, fake_gcs_client: FakeGCSClient
):
    first = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("original.pdf", PDF_MAGIC_BYTES)
    )
    first_object_name = first.json()["data"]["file"]["object_name"]

    second = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("copy.pdf", PDF_MAGIC_BYTES)
    )
    assert second.status_code == 201
    body = second.json()["data"]
    assert body["is_duplicate"] is True
    assert body["file"]["object_name"] == first_object_name

    # Only one object was ever actually written to storage.
    bucket = fake_gcs_client.bucket(body["file"]["bucket_name"])
    assert len(bucket.store) == 1


@pytest.mark.asyncio
async def test_download_file_roundtrip(authed_client: AsyncClient):
    upload = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("report.pdf", PDF_MAGIC_BYTES)
    )
    file_id = upload.json()["data"]["file"]["id"]

    response = await authed_client.get(f"/api/v1/files/{file_id}/download")
    assert response.status_code == 200
    assert response.content == PDF_MAGIC_BYTES
    assert "attachment" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_download_range_request(authed_client: AsyncClient):
    upload = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("report.pdf", PDF_MAGIC_BYTES)
    )
    file_id = upload.json()["data"]["file"]["id"]

    response = await authed_client.get(
        f"/api/v1/files/{file_id}/download", headers={"Range": "bytes=0-9"}
    )
    assert response.status_code == 206
    assert response.content == PDF_MAGIC_BYTES[:10]
    assert response.headers["content-range"] == f"bytes 0-9/{len(PDF_MAGIC_BYTES)}"


@pytest.mark.asyncio
async def test_download_nonexistent_file_returns_404(authed_client: AsyncClient):
    response = await authed_client.get("/api/v1/files/00000000-0000-0000-0000-000000000000/download")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_download_reserved_metadata_only_file_returns_404(authed_client: AsyncClient):
    """A Phase-2-style metadata-only row (no bytes ever uploaded) must not be downloadable."""
    created = (
        await authed_client.post("/api/v1/metadata", json={"original_filename": "placeholder.pdf"})
    ).json()["data"]

    response = await authed_client.get(f"/api/v1/files/{created['id']}/download")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_signed_url_generation(authed_client: AsyncClient):
    upload = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("report.pdf", PDF_MAGIC_BYTES)
    )
    file_id = upload.json()["data"]["file"]["id"]

    response = await authed_client.get(f"/api/v1/files/{file_id}/signed-url")
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["url"].startswith("https://storage.fake.test/")
    assert body["expires_in_minutes"] == 15


@pytest.mark.asyncio
async def test_signed_url_custom_expiration(authed_client: AsyncClient):
    upload = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("report.pdf", PDF_MAGIC_BYTES)
    )
    file_id = upload.json()["data"]["file"]["id"]

    response = await authed_client.get(
        f"/api/v1/files/{file_id}/signed-url", params={"expires_in_minutes": 60}
    )
    assert response.json()["data"]["expires_in_minutes"] == 60


@pytest.mark.asyncio
async def test_replace_file_bumps_version_and_swaps_object(
    authed_client: AsyncClient, fake_gcs_client: FakeGCSClient
):
    upload = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("report.pdf", PDF_MAGIC_BYTES)
    )
    file_data = upload.json()["data"]["file"]
    file_id, old_object_name, bucket_name = file_data["id"], file_data["object_name"], file_data["bucket_name"]

    new_content = PDF_MAGIC_BYTES + b"extra-bytes-for-v2"
    response = await authed_client.put(
        f"/api/v1/files/{file_id}/replace", files=_upload_payload("report.pdf", new_content)
    )
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["version"] == 2
    assert body["size"] == len(new_content)
    assert body["object_name"] != old_object_name

    # Old object was cleaned up since nothing else references it.
    bucket = fake_gcs_client.bucket(bucket_name)
    assert old_object_name not in bucket.store
    assert body["object_name"] in bucket.store

    download = await authed_client.get(f"/api/v1/files/{file_id}/download")
    assert download.content == new_content


@pytest.mark.asyncio
async def test_permanent_delete_removes_object_and_row(
    authed_client: AsyncClient, fake_gcs_client: FakeGCSClient
):
    upload = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("report.pdf", PDF_MAGIC_BYTES)
    )
    file_data = upload.json()["data"]["file"]
    file_id, object_name, bucket_name = file_data["id"], file_data["object_name"], file_data["bucket_name"]

    await authed_client.delete(f"/api/v1/metadata/{file_id}")  # soft delete (Phase 2 endpoint)

    response = await authed_client.delete(f"/api/v1/files/{file_id}/permanent")
    assert response.status_code == 200

    bucket = fake_gcs_client.bucket(bucket_name)
    assert object_name not in bucket.store

    get_response = await authed_client.get(f"/api/v1/metadata/{file_id}")
    assert get_response.status_code == 404


@pytest.mark.asyncio
async def test_permanent_delete_requires_trash_first(authed_client: AsyncClient):
    upload = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("report.pdf", PDF_MAGIC_BYTES)
    )
    file_id = upload.json()["data"]["file"]["id"]

    response = await authed_client.delete(f"/api/v1/files/{file_id}/permanent")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_permanent_delete_keeps_shared_object_for_deduplicated_file(
    authed_client: AsyncClient, fake_gcs_client: FakeGCSClient
):
    """Deleting one of two rows that share a deduplicated object must not delete the shared bytes."""
    first = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("original.pdf", PDF_MAGIC_BYTES)
    )
    second = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("copy.pdf", PDF_MAGIC_BYTES)
    )
    second_data = second.json()["data"]["file"]
    object_name, bucket_name = second_data["object_name"], second_data["bucket_name"]

    await authed_client.delete(f"/api/v1/metadata/{second_data['id']}")
    await authed_client.delete(f"/api/v1/files/{second_data['id']}/permanent")

    bucket = fake_gcs_client.bucket(bucket_name)
    assert object_name in bucket.store  # still referenced by `first`

    download = await authed_client.get(f"/api/v1/files/{first.json()['data']['file']['id']}/download")
    assert download.status_code == 200


@pytest.mark.asyncio
async def test_upload_rollback_deletes_object_on_metadata_failure(
    authed_client: AsyncClient, fake_gcs_client: FakeGCSClient, monkeypatch: pytest.MonkeyPatch
):
    """
    Simulates a metadata-persistence failure AFTER the GCS upload already
    succeeded, and asserts the just-uploaded object is rolled back
    (deleted) rather than left orphaned in the bucket.
    """
    from app.repositories.file_metadata_repository import FileMetadataRepository

    async def _boom(self, entity):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(FileMetadataRepository, "add", _boom)

    # An unhandled exception raised from inside `BaseHTTPMiddleware` (our
    # request-context/security-headers middleware) propagates through
    # httpx's ASGITransport as a real Python exception rather than a
    # clean 500 response — that's an httpx/Starlette interaction, not
    # something under test here. What matters is that FileUploadService's
    # rollback ran before re-raising, which we assert on below.
    with pytest.raises(RuntimeError, match="simulated database failure"):
        await authed_client.post("/api/v1/files/upload", files=_upload_payload("report.pdf", PDF_MAGIC_BYTES))

    for bucket in fake_gcs_client._buckets.values():
        assert bucket.store == {}


@pytest.mark.asyncio
async def test_upload_storage_failure_returns_502(
    authed_client: AsyncClient, fake_gcs_client: FakeGCSClient, monkeypatch: pytest.MonkeyPatch
):
    from google.api_core import exceptions as gcs_exceptions

    from tests.fakes.fake_gcs import FakeBlob

    def _boom(self, stream, content_type=None, size=None):
        raise gcs_exceptions.ServiceUnavailable("simulated outage")

    monkeypatch.setattr(FakeBlob, "upload_from_file", _boom)

    response = await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("report.pdf", PDF_MAGIC_BYTES)
    )
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_upload_scoped_to_owner(client: AsyncClient):
    """A second user must never be able to download another user's file."""
    user_a = {"first_name": "A", "last_name": "One", "email": "a@nimbusfs.io", "password": "StrongP@ss1!"}
    user_b = {"first_name": "B", "last_name": "Two", "email": "b@nimbusfs.io", "password": "StrongP@ss2!"}

    await client.post("/api/v1/auth/register", json=user_a)
    login_a = await client.post(
        "/api/v1/auth/login", data={"username": user_a["email"], "password": user_a["password"]}
    )
    token_a = login_a.json()["data"]["access_token"]
    upload = await client.post(
        "/api/v1/files/upload",
        files=_upload_payload("a-secret.pdf", PDF_MAGIC_BYTES),
        headers={"Authorization": f"Bearer {token_a}"},
    )
    file_id = upload.json()["data"]["file"]["id"]

    await client.post("/api/v1/auth/register", json=user_b)
    login_b = await client.post(
        "/api/v1/auth/login", data={"username": user_b["email"], "password": user_b["password"]}
    )
    token_b = login_b.json()["data"]["access_token"]

    response = await client.get(
        f"/api/v1/files/{file_id}/download", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert response.status_code == 404
