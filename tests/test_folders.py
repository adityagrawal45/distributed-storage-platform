"""Tests for folder CRUD, hierarchy, move/rename, tree, breadcrumb, and trash."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_top_level_folder(authed_client: AsyncClient):
    response = await authed_client.post("/api/v1/folders", json={"name": "Documents"})
    assert response.status_code == 201
    body = response.json()["data"]
    assert body["name"] == "Documents"
    assert body["path"] == "/Documents"
    assert body["level"] == 0
    assert body["is_root"] is True
    assert body["parent_folder_id"] is None


@pytest.mark.asyncio
async def test_create_nested_folder(authed_client: AsyncClient):
    parent = (await authed_client.post("/api/v1/folders", json={"name": "Documents"})).json()["data"]
    response = await authed_client.post(
        "/api/v1/folders", json={"name": "Projects", "parent_folder_id": parent["id"]}
    )
    assert response.status_code == 201
    body = response.json()["data"]
    assert body["path"] == "/Documents/Projects"
    assert body["level"] == 1
    assert body["is_root"] is False


@pytest.mark.asyncio
async def test_duplicate_folder_name_same_parent_rejected(authed_client: AsyncClient):
    await authed_client.post("/api/v1/folders", json={"name": "Documents"})
    response = await authed_client.post("/api/v1/folders", json={"name": "Documents"})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_same_name_different_parent_allowed(authed_client: AsyncClient):
    a = (await authed_client.post("/api/v1/folders", json={"name": "A"})).json()["data"]
    b = (await authed_client.post("/api/v1/folders", json={"name": "B"})).json()["data"]

    r1 = await authed_client.post("/api/v1/folders", json={"name": "Reports", "parent_folder_id": a["id"]})
    r2 = await authed_client.post("/api/v1/folders", json={"name": "Reports", "parent_folder_id": b["id"]})
    assert r1.status_code == 201
    assert r2.status_code == 201


@pytest.mark.asyncio
async def test_empty_folder_name_rejected(authed_client: AsyncClient):
    response = await authed_client.post("/api/v1/folders", json={"name": "   "})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_invalid_characters_in_folder_name_rejected(authed_client: AsyncClient):
    response = await authed_client.post("/api/v1/folders", json={"name": "bad/name"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_folder_by_id(authed_client: AsyncClient):
    created = (await authed_client.post("/api/v1/folders", json={"name": "Documents"})).json()["data"]
    response = await authed_client.get(f"/api/v1/folders/{created['id']}")
    assert response.status_code == 200
    assert response.json()["data"]["id"] == created["id"]


@pytest.mark.asyncio
async def test_get_nonexistent_folder_returns_404(authed_client: AsyncClient):
    response = await authed_client.get("/api/v1/folders/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_rename_folder(authed_client: AsyncClient):
    created = (await authed_client.post("/api/v1/folders", json={"name": "Old"})).json()["data"]
    response = await authed_client.put(f"/api/v1/folders/{created['id']}", json={"name": "New"})
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["name"] == "New"
    assert body["path"] == "/New"


@pytest.mark.asyncio
async def test_rename_cascades_to_children_paths(authed_client: AsyncClient):
    parent = (await authed_client.post("/api/v1/folders", json={"name": "Old"})).json()["data"]
    child = (
        await authed_client.post("/api/v1/folders", json={"name": "Child", "parent_folder_id": parent["id"]})
    ).json()["data"]

    await authed_client.put(f"/api/v1/folders/{parent['id']}", json={"name": "New"})

    child_response = await authed_client.get(f"/api/v1/folders/{child['id']}")
    assert child_response.json()["data"]["path"] == "/New/Child"


@pytest.mark.asyncio
async def test_move_folder(authed_client: AsyncClient):
    a = (await authed_client.post("/api/v1/folders", json={"name": "A"})).json()["data"]
    b = (await authed_client.post("/api/v1/folders", json={"name": "B"})).json()["data"]

    response = await authed_client.post(f"/api/v1/folders/{a['id']}/move", json={"new_parent_folder_id": b["id"]})
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["path"] == "/B/A"
    assert body["level"] == 1


@pytest.mark.asyncio
async def test_cannot_move_folder_into_itself(authed_client: AsyncClient):
    a = (await authed_client.post("/api/v1/folders", json={"name": "A"})).json()["data"]
    response = await authed_client.post(f"/api/v1/folders/{a['id']}/move", json={"new_parent_folder_id": a["id"]})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_cannot_move_parent_into_its_own_child(authed_client: AsyncClient):
    parent = (await authed_client.post("/api/v1/folders", json={"name": "Parent"})).json()["data"]
    child = (
        await authed_client.post("/api/v1/folders", json={"name": "Child", "parent_folder_id": parent["id"]})
    ).json()["data"]

    response = await authed_client.post(
        f"/api/v1/folders/{parent['id']}/move", json={"new_parent_folder_id": child["id"]}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_get_folder_tree(authed_client: AsyncClient):
    parent = (await authed_client.post("/api/v1/folders", json={"name": "Documents"})).json()["data"]
    await authed_client.post("/api/v1/folders", json={"name": "Projects", "parent_folder_id": parent["id"]})

    response = await authed_client.get("/api/v1/folders/tree")
    assert response.status_code == 200
    tree = response.json()["data"]
    assert len(tree) == 1
    assert tree[0]["name"] == "Documents"
    assert len(tree[0]["children"]) == 1
    assert tree[0]["children"][0]["name"] == "Projects"


@pytest.mark.asyncio
async def test_get_breadcrumb(authed_client: AsyncClient):
    a = (await authed_client.post("/api/v1/folders", json={"name": "A"})).json()["data"]
    b = (await authed_client.post("/api/v1/folders", json={"name": "B", "parent_folder_id": a["id"]})).json()["data"]
    c = (await authed_client.post("/api/v1/folders", json={"name": "C", "parent_folder_id": b["id"]})).json()["data"]

    response = await authed_client.get("/api/v1/folders/breadcrumb", params={"folder_id": c["id"]})
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert [item["name"] for item in items] == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_delete_and_restore_folder(authed_client: AsyncClient):
    folder = (await authed_client.post("/api/v1/folders", json={"name": "Documents"})).json()["data"]

    delete_response = await authed_client.delete(f"/api/v1/folders/{folder['id']}")
    assert delete_response.status_code == 200

    # No longer visible in the active view.
    get_response = await authed_client.get(f"/api/v1/folders/{folder['id']}")
    assert get_response.status_code == 404

    # Appears in trash.
    trash_response = await authed_client.get("/api/v1/folders/trash")
    trash_ids = [f["id"] for f in trash_response.json()["data"]]
    assert folder["id"] in trash_ids

    restore_response = await authed_client.post(f"/api/v1/folders/{folder['id']}/restore")
    assert restore_response.status_code == 200

    get_after_restore = await authed_client.get(f"/api/v1/folders/{folder['id']}")
    assert get_after_restore.status_code == 200


@pytest.mark.asyncio
async def test_permanent_delete_requires_trash_first(authed_client: AsyncClient):
    folder = (await authed_client.post("/api/v1/folders", json={"name": "Documents"})).json()["data"]
    response = await authed_client.delete(f"/api/v1/folders/{folder['id']}/permanent")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_permanent_delete_after_trash_succeeds(authed_client: AsyncClient):
    folder = (await authed_client.post("/api/v1/folders", json={"name": "Documents"})).json()["data"]
    await authed_client.delete(f"/api/v1/folders/{folder['id']}")

    response = await authed_client.delete(f"/api/v1/folders/{folder['id']}/permanent")
    assert response.status_code == 200

    # Even a restore attempt should now 404 — it's really gone.
    restore_response = await authed_client.post(f"/api/v1/folders/{folder['id']}/restore")
    assert restore_response.status_code == 404


@pytest.mark.asyncio
async def test_folders_require_authentication(client: AsyncClient):
    response = await client.get("/api/v1/folders")
    assert response.status_code == 401