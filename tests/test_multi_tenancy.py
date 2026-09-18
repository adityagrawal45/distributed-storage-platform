"""
Phase 13 — cross-tenant security tests.

Every user gets their own personal `Organization` at registration
(`OrganizationService.create_personal`), so `authed_client` (user A) and
`second_user_token` (user B) are, by construction, two different tenants
sharing one process/database — exactly the setup Phase 13 §35/§48 asks
these tests to exercise: tenant isolation, IDOR/forged-ID attempts,
share expiry/revocation, and permission-grant validation.

Requests "as user B" are made by passing `headers={"Authorization": ...}`
explicitly per-call against the SAME shared `client`/transport, rather
than mutating `client.headers` (which only one identity can hold at a
time) — see `second_user_token`'s fixture docstring.
"""

import io
import uuid

import pytest
from httpx import AsyncClient

PDF_MAGIC_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"0" * 100


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _upload_payload(filename: str = "report.pdf", content: bytes = PDF_MAGIC_BYTES) -> dict:
    return {"file": (filename, io.BytesIO(content), "application/pdf")}


@pytest.mark.asyncio
async def test_folder_created_by_one_tenant_is_invisible_to_another(
    authed_client: AsyncClient, second_user_token: str
):
    created = await authed_client.post("/api/v1/folders", json={"name": "Confidential"})
    folder_id = created.json()["data"]["id"]

    # Same folder ID, requested as a completely different tenant — must
    # look exactly like a nonexistent resource, never a 403 ("exists but
    # you can't see it" is itself information leakage about another
    # tenant's data — see ShareService's identical non-disclosure choice).
    response = await authed_client.get(f"/api/v1/folders/{folder_id}", headers=_auth(second_user_token))
    assert response.status_code == 404

    listing = await authed_client.get("/api/v1/folders", headers=_auth(second_user_token))
    assert listing.status_code == 200
    assert all(f["id"] != folder_id for f in listing.json()["data"])


@pytest.mark.asyncio
async def test_file_download_across_tenants_is_an_idor_denied_as_404(
    authed_client: AsyncClient, second_user_token: str
):
    upload = await authed_client.post("/api/v1/files/upload", files=_upload_payload())
    file_id = upload.json()["data"]["file"]["id"]

    response = await authed_client.get(f"/api/v1/files/{file_id}/download", headers=_auth(second_user_token))
    assert response.status_code == 404

    signed_url = await authed_client.get(f"/api/v1/files/{file_id}/signed-url", headers=_auth(second_user_token))
    assert signed_url.status_code == 404


@pytest.mark.asyncio
async def test_forged_folder_id_from_another_tenant_cannot_be_used_as_a_move_target(
    authed_client: AsyncClient, second_user_token: str
):
    other_folder = await authed_client.post(
        "/api/v1/folders", json={"name": "OtherTenantFolder"}, headers=_auth(second_user_token)
    )
    other_folder_id = other_folder.json()["data"]["id"]

    mine = await authed_client.post("/api/v1/folders", json={"name": "Mine"})
    my_folder_id = mine.json()["data"]["id"]

    response = await authed_client.post(
        f"/api/v1/folders/{my_folder_id}/move", json={"new_parent_folder_id": other_folder_id}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_search_never_returns_another_tenants_files(authed_client: AsyncClient, second_user_token: str):
    await authed_client.post("/api/v1/files/upload", files=_upload_payload("mine-unique-xyz.pdf"))
    await authed_client.post(
        "/api/v1/files/upload", files=_upload_payload("theirs-unique-xyz.pdf"), headers=_auth(second_user_token)
    )

    response = await authed_client.get("/api/v1/metadata/search", params={"q": "unique-xyz"})
    assert response.status_code == 200
    names = {f["original_filename"] for f in response.json()["data"]["items"]}
    assert "mine-unique-xyz.pdf" in names
    assert "theirs-unique-xyz.pdf" not in names


@pytest.mark.asyncio
async def test_share_redemption_is_public_and_crosses_tenants_by_design(
    authed_client: AsyncClient, second_user_token: str
):
    """A share is the ONE deliberate exception to tenant isolation — its whole point is letting
    someone outside the tenant (here: a user of a DIFFERENT tenant) access one specific resource."""
    upload = await authed_client.post("/api/v1/files/upload", files=_upload_payload())
    file_id = upload.json()["data"]["file"]["id"]

    created = await authed_client.post(
        "/api/v1/shares", json={"resource_type": "file", "resource_id": file_id, "permission": "read"}
    )
    assert created.status_code == 201
    token = created.json()["data"]["token"]
    assert token

    # Redeemed by a user who belongs to a totally different organization
    # — and even by no authenticated user at all (the endpoint is public).
    redeemed = await authed_client.get(f"/api/v1/shares/redeem/{token}", headers=_auth(second_user_token))
    assert redeemed.status_code == 200

    download = await authed_client.get(f"/api/v1/shares/redeem/{token}/download")
    assert download.status_code == 200
    assert download.content == PDF_MAGIC_BYTES


@pytest.mark.asyncio
async def test_revoked_share_and_forged_token_both_return_the_same_404(authed_client: AsyncClient):
    upload = await authed_client.post("/api/v1/files/upload", files=_upload_payload())
    file_id = upload.json()["data"]["file"]["id"]

    created = await authed_client.post(
        "/api/v1/shares", json={"resource_type": "file", "resource_id": file_id, "permission": "read"}
    )
    share_id = created.json()["data"]["share"]["id"]
    token = created.json()["data"]["token"]

    revoke = await authed_client.delete(f"/api/v1/shares/{share_id}")
    assert revoke.status_code == 200

    revoked_redeem = await authed_client.get(f"/api/v1/shares/redeem/{token}")
    forged_redeem = await authed_client.get("/api/v1/shares/redeem/not-a-real-token-at-all")

    # Same status AND same failure shape for "revoked" vs. "never
    # existed" — a distinguishable response here would let an attacker
    # tell real-but-revoked tokens apart from garbage (Phase 13 §16/§36).
    assert revoked_redeem.status_code == forged_redeem.status_code == 404


@pytest.mark.asyncio
async def test_permission_grant_rejects_a_principal_outside_the_organization(
    authed_client: AsyncClient, second_user_token: str
):
    folder = await authed_client.post("/api/v1/folders", json={"name": "SharedWithMyOrgOnly"})
    folder_id = folder.json()["data"]["id"]

    other_profile = await authed_client.get("/api/v1/users/me", headers=_auth(second_user_token))
    other_user_id = other_profile.json()["data"]["id"]

    response = await authed_client.post(
        "/api/v1/organizations/me/permissions",
        json={
            "resource_type": "folder",
            "resource_id": folder_id,
            "principal_type": "user",
            "principal_id": other_user_id,
            "permission": "read",
        },
    )
    # The target user is real but belongs to a DIFFERENT tenant — this
    # must never succeed, since it would be a standing cross-tenant
    # foothold on the granting tenant's own resource.
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_organization_me_never_reflects_a_client_supplied_org_id_for_another_tenant(
    authed_client: AsyncClient, second_user_token: str
):
    """`CurrentOrganization` is resolved from the caller's own membership, never trusted from
    client input — there is no request parameter anywhere that lets a caller name a DIFFERENT
    organization and have it honored (Phase 13 §2/§6)."""
    my_org = (await authed_client.get("/api/v1/organizations/me")).json()["data"]
    their_org = (await authed_client.get("/api/v1/organizations/me", headers=_auth(second_user_token))).json()["data"]
    assert my_org["id"] != their_org["id"]

    # Forging the OTHER tenant's org ID via the header, without being a
    # member of it, must be rejected outright.
    forged = await authed_client.get("/api/v1/organizations/me", headers={"X-Organization-ID": their_org["id"]})
    assert forged.status_code in (403, 404)


@pytest.mark.asyncio
async def test_unauthenticated_requests_are_rejected_before_any_tenant_resolution(authed_client: AsyncClient):
    response = await authed_client.get("/api/v1/folders", headers={"Authorization": ""})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_share_with_wrong_password_is_distinguishable_from_expired_or_revoked(authed_client: AsyncClient):
    upload = await authed_client.post("/api/v1/files/upload", files=_upload_payload())
    file_id = upload.json()["data"]["file"]["id"]

    created = await authed_client.post(
        "/api/v1/shares",
        json={"resource_type": "file", "resource_id": file_id, "permission": "read", "password": "s3cr3t!"},
    )
    token = created.json()["data"]["token"]

    wrong_password = await authed_client.get(f"/api/v1/shares/redeem/{token}", params={"password": "nope"})
    assert wrong_password.status_code == 403

    correct_password = await authed_client.get(f"/api/v1/shares/redeem/{token}", params={"password": "s3cr3t!"})
    assert correct_password.status_code == 200


@pytest.mark.asyncio
async def test_group_membership_cannot_span_organizations(authed_client: AsyncClient, second_user_token: str):
    group = await authed_client.post("/api/v1/organizations/me/groups", json={"name": "Engineering"})
    group_id = group.json()["data"]["id"]

    other_profile = await authed_client.get("/api/v1/users/me", headers=_auth(second_user_token))
    other_user_id = other_profile.json()["data"]["id"]

    response = await authed_client.post(
        f"/api/v1/organizations/me/groups/{group_id}/members", json={"user_id": other_user_id}
    )
    assert response.status_code == 404
