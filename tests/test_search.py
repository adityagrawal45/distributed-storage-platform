"""Tests for file search, pagination, sorting, and filtering."""

import pytest
from httpx import AsyncClient


async def _create_file(client: AsyncClient, name: str, size: int = 0, mime_type: str | None = None) -> dict:
    payload = {"original_filename": name, "size": size}
    if mime_type:
        payload["mime_type"] = mime_type
    response = await client.post("/api/v1/metadata", json=payload)
    return response.json()["data"]


@pytest.mark.asyncio
async def test_search_by_filename(authed_client: AsyncClient):
    await _create_file(authed_client, "quarterly-report.pdf")
    await _create_file(authed_client, "vacation-photo.png")

    response = await authed_client.get("/api/v1/metadata/search", params={"q": "report"})
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["original_filename"] == "quarterly-report.pdf"


@pytest.mark.asyncio
async def test_search_by_folder_name(authed_client: AsyncClient):
    folder = (await authed_client.post("/api/v1/folders", json={"name": "TaxDocuments"})).json()["data"]
    await authed_client.post(
        "/api/v1/metadata", json={"original_filename": "unrelated.txt", "folder_id": folder["id"]}
    )
    await _create_file(authed_client, "other.txt")

    response = await authed_client.get("/api/v1/metadata/search", params={"q": "TaxDocuments"})
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["original_filename"] == "unrelated.txt"


@pytest.mark.asyncio
async def test_filter_by_extension(authed_client: AsyncClient):
    await _create_file(authed_client, "a.pdf")
    await _create_file(authed_client, "b.png")

    response = await authed_client.get("/api/v1/metadata/search", params={"extension": "pdf"})
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["extension"] == "pdf"


@pytest.mark.asyncio
async def test_filter_by_mime_type(authed_client: AsyncClient):
    await _create_file(authed_client, "a.pdf", mime_type="application/pdf")
    await _create_file(authed_client, "b.txt", mime_type="text/plain")

    response = await authed_client.get("/api/v1/metadata/search", params={"mime_type": "text/plain"})
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["mime_type"] == "text/plain"


@pytest.mark.asyncio
async def test_filter_deleted_only(authed_client: AsyncClient):
    active = await _create_file(authed_client, "active.txt")
    trashed = await _create_file(authed_client, "trashed.txt")
    await authed_client.delete(f"/api/v1/metadata/{trashed['id']}")

    response = await authed_client.get("/api/v1/metadata/search", params={"is_deleted": "true"})
    items = response.json()["data"]["items"]
    ids = [i["id"] for i in items]
    assert trashed["id"] in ids
    assert active["id"] not in ids


@pytest.mark.asyncio
async def test_sort_by_size_ascending(authed_client: AsyncClient):
    await _create_file(authed_client, "big.bin", size=1000)
    await _create_file(authed_client, "small.bin", size=10)

    response = await authed_client.get(
        "/api/v1/metadata/search", params={"sort_by": "size", "sort_order": "asc"}
    )
    items = response.json()["data"]["items"]
    sizes = [i["size"] for i in items]
    assert sizes == sorted(sizes)


@pytest.mark.asyncio
async def test_sort_by_size_descending(authed_client: AsyncClient):
    await _create_file(authed_client, "big.bin", size=1000)
    await _create_file(authed_client, "small.bin", size=10)

    response = await authed_client.get(
        "/api/v1/metadata/search", params={"sort_by": "size", "sort_order": "desc"}
    )
    items = response.json()["data"]["items"]
    sizes = [i["size"] for i in items]
    assert sizes == sorted(sizes, reverse=True)


@pytest.mark.asyncio
async def test_pagination(authed_client: AsyncClient):
    for i in range(5):
        await _create_file(authed_client, f"file-{i}.txt")

    page_1 = await authed_client.get("/api/v1/metadata/search", params={"page": 1, "page_size": 2})
    body_1 = page_1.json()["data"]
    assert len(body_1["items"]) == 2
    assert body_1["total"] == 5
    assert body_1["total_pages"] == 3
    assert body_1["has_next"] is True
    assert body_1["has_previous"] is False

    page_3 = await authed_client.get("/api/v1/metadata/search", params={"page": 3, "page_size": 2})
    body_3 = page_3.json()["data"]
    assert len(body_3["items"]) == 1
    assert body_3["has_next"] is False
    assert body_3["has_previous"] is True


@pytest.mark.asyncio
async def test_page_size_exceeding_max_rejected(authed_client: AsyncClient):
    response = await authed_client.get("/api/v1/metadata/search", params={"page_size": 1000})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_search_scoped_to_owner(client: AsyncClient):
    """A second user's files must never appear in another user's search results."""
    user_a = {"first_name": "A", "last_name": "One", "email": "a@nimbusfs.io", "password": "StrongP@ss1!"}
    user_b = {"first_name": "B", "last_name": "Two", "email": "b@nimbusfs.io", "password": "StrongP@ss2!"}

    await client.post("/api/v1/auth/register", json=user_a)
    login_a = await client.post(
        "/api/v1/auth/login", data={"username": user_a["email"], "password": user_a["password"]}
    )
    token_a = login_a.json()["data"]["access_token"]
    await client.post(
        "/api/v1/metadata",
        json={"original_filename": "a-secret.txt"},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    await client.post("/api/v1/auth/register", json=user_b)
    login_b = await client.post(
        "/api/v1/auth/login", data={"username": user_b["email"], "password": user_b["password"]}
    )
    token_b = login_b.json()["data"]["access_token"]

    response = await client.get(
        "/api/v1/metadata/search", headers={"Authorization": f"Bearer {token_b}"}
    )
    items = response.json()["data"]["items"]
    assert all(i["original_filename"] != "a-secret.txt" for i in items)