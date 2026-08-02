"""Tests for file metadata CRUD, rename, move, versioning, and trash."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_metadata(authed_client: AsyncClient):
    response = await authed_client.post(
        "/api/v1/metadata",
        json={"original_filename": "report.pdf", "mime_type": "application/pdf", "size": 1024},
    )
    assert response.status_code == 201
    body = response.json()["data"]
    assert body["original_filename"] == "report.pdf"
    assert body["extension"] == "pdf"
    assert body["version"] == 1
    assert body["status"] == "reserved"
    assert body["stored_filename"]  # a name was reserved


@pytest.mark.asyncio
async def test_create_metadata_in_folder(authed_client: AsyncClient):
    folder = (await authed_client.post("/api/v1/folders", json={"name": "Documents"})).json()["data"]
    response = await authed_client.post(
        "/api/v1/metadata", json={"original_filename": "report.pdf", "folder_id": folder["id"]}
    )
    assert response.status_code == 201
    assert response.json()["data"]["folder_id"] == folder["id"]


@pytest.mark.asyncio
async def test_create_metadata_in_nonexistent_folder_returns_404(authed_client: AsyncClient):
    response = await authed_client.post(
        "/api/v1/metadata",
        json={"original_filename": "report.pdf", "folder_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_filename_same_folder_rejected(authed_client: AsyncClient):
    await authed_client.post("/api/v1/metadata", json={"original_filename": "report.pdf"})
    response = await authed_client.post("/api/v1/metadata", json={"original_filename": "report.pdf"})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_get_metadata_by_id(authed_client: AsyncClient):
    created = (await authed_client.post("/api/v1/metadata", json={"original_filename": "report.pdf"})).json()["data"]
    response = await authed_client.get(f"/api/v1/metadata/{created['id']}")
    assert response.status_code == 200
    assert response.json()["data"]["id"] == created["id"]


@pytest.mark.asyncio
async def test_rename_file(authed_client: AsyncClient):
    created = (await authed_client.post("/api/v1/metadata", json={"original_filename": "report.pdf"})).json()["data"]
    response = await authed_client.post(f"/api/v1/metadata/{created['id']}/rename", json={"name": "final.pdf"})
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["original_filename"] == "final.pdf"
    assert body["extension"] == "pdf"


@pytest.mark.asyncio
async def test_move_file(authed_client: AsyncClient):
    folder = (await authed_client.post("/api/v1/folders", json={"name": "Documents"})).json()["data"]
    created = (await authed_client.post("/api/v1/metadata", json={"original_filename": "report.pdf"})).json()["data"]

    response = await authed_client.post(f"/api/v1/metadata/{created['id']}/move", json={"new_folder_id": folder["id"]})
    assert response.status_code == 200
    assert response.json()["data"]["folder_id"] == folder["id"]


@pytest.mark.asyncio
async def test_update_metadata_size_creates_new_version(authed_client: AsyncClient):
    created = (
        await authed_client.post("/api/v1/metadata", json={"original_filename": "report.pdf", "size": 100})
    ).json()["data"]

    response = await authed_client.put(f"/api/v1/metadata/{created['id']}", json={"size": 200})
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["size"] == 200
    assert body["version"] == 2

    versions_response = await authed_client.get(f"/api/v1/metadata/{created['id']}/versions")
    versions = versions_response.json()["data"]
    assert len(versions) == 2
    assert {v["version"] for v in versions} == {1, 2}


@pytest.mark.asyncio
async def test_update_metadata_mime_only_does_not_bump_version(authed_client: AsyncClient):
    created = (await authed_client.post("/api/v1/metadata", json={"original_filename": "report.pdf"})).json()["data"]

    response = await authed_client.put(
        f"/api/v1/metadata/{created['id']}", json={"mime_type": "application/pdf"}
    )
    assert response.status_code == 200
    assert response.json()["data"]["version"] == 1


@pytest.mark.asyncio
async def test_delete_and_restore_file(authed_client: AsyncClient):
    created = (await authed_client.post("/api/v1/metadata", json={"original_filename": "report.pdf"})).json()["data"]

    delete_response = await authed_client.delete(f"/api/v1/metadata/{created['id']}")
    assert delete_response.status_code == 200

    get_response = await authed_client.get(f"/api/v1/metadata/{created['id']}")
    assert get_response.status_code == 404

    trash_response = await authed_client.get("/api/v1/metadata/trash")
    trash_ids = [f["id"] for f in trash_response.json()["data"]]
    assert created["id"] in trash_ids

    restore_response = await authed_client.post(f"/api/v1/metadata/{created['id']}/restore")
    assert restore_response.status_code == 200

    get_after_restore = await authed_client.get(f"/api/v1/metadata/{created['id']}")
    assert get_after_restore.status_code == 200


@pytest.mark.asyncio
async def test_permanent_delete_requires_trash_first(authed_client: AsyncClient):
    created = (await authed_client.post("/api/v1/metadata", json={"original_filename": "report.pdf"})).json()["data"]
    response = await authed_client.delete(f"/api/v1/metadata/{created['id']}/permanent")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_combined_trash_view(authed_client: AsyncClient):
    folder = (await authed_client.post("/api/v1/folders", json={"name": "Documents"})).json()["data"]
    file = (await authed_client.post("/api/v1/metadata", json={"original_filename": "report.pdf"})).json()["data"]

    await authed_client.delete(f"/api/v1/folders/{folder['id']}")
    await authed_client.delete(f"/api/v1/metadata/{file['id']}")

    response = await authed_client.get("/api/v1/trash")
    assert response.status_code == 200
    body = response.json()["data"]
    assert any(f["id"] == folder["id"] for f in body["folders"])
    assert any(f["id"] == file["id"] for f in body["files"])