"""
Secure sharing links (Phase 13 §14-18).

Creating/listing/revoking a share is an authenticated, org-scoped
operation gated by `PermissionResolver` (the caller must own the
resource or hold `SHARE`/`MANAGE` on it — never just "any org member
can share anything"). Redemption is the deliberate exception: it is a
PUBLIC, unauthenticated endpoint (a share token is itself the
credential — see `ShareService`'s module docstring) and therefore does
NOT depend on `CurrentUser`/`CurrentOrganization` at all.
"""

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse

from app.core.enums import Permission, ResourceType
from app.dependencies.auth import CurrentUser
from app.dependencies.organization import CurrentOrganization
from app.dependencies.providers import (
    FileMetadataRepositoryDep,
    FileUploadServiceDep,
    FolderRepositoryDep,
    PermissionResolverDep,
    ShareServiceDep,
)
from app.exceptions.custom_exceptions import FileNotFoundException, FolderNotFoundException, ValidationException
from app.models.file_metadata import FileMetadata
from app.models.folder import Folder
from app.schemas.pagination import Page, PaginationParams
from app.schemas.response import APIResponse
from app.schemas.share import ShareCreate, ShareCreateResponse, ShareRead

router = APIRouter(prefix="/shares", tags=["Sharing"])


async def _load_resource(
    resource_type: ResourceType,
    resource_id: uuid.UUID,
    organization_id: uuid.UUID,
    folders: FolderRepositoryDep,
    files: FileMetadataRepositoryDep,
) -> Folder | FileMetadata:
    if resource_type == ResourceType.FOLDER:
        folder = await folders.get_in_organization(resource_id, organization_id)
        if folder is None:
            raise FolderNotFoundException()
        return folder
    file = await files.get_in_organization(resource_id, organization_id)
    if file is None:
        raise FileNotFoundException()
    return file


@router.post(
    "",
    response_model=APIResponse[ShareCreateResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Create a secure sharing link for a folder or file",
    description="The raw `token` in the response is shown exactly once and cannot be retrieved again.",
)
async def create_share(
    payload: ShareCreate,
    current_user: CurrentUser,
    org: CurrentOrganization,
    share_service: ShareServiceDep,
    resolver: PermissionResolverDep,
    folders: FolderRepositoryDep,
    files: FileMetadataRepositoryDep,
) -> APIResponse[ShareCreateResponse]:
    resource = await _load_resource(payload.resource_type, payload.resource_id, org.organization_id, folders, files)
    await resolver.require(
        user_id=current_user.id, membership=org.membership, resource=resource, permission=Permission.SHARE
    )
    share, raw_token = await share_service.create(
        actor_user_id=current_user.id,
        organization_id=org.organization_id,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        permission=payload.permission,
        expires_in_hours=payload.expires_in_hours,
        password=payload.password,
        max_downloads=payload.max_downloads,
    )
    return APIResponse(
        message="Share created successfully.",
        data=ShareCreateResponse(share=ShareRead.from_share(share), token=raw_token),
    )


@router.get("", response_model=APIResponse[Page[ShareRead]], summary="List shares created in this organization")
async def list_shares(
    current_user: CurrentUser,
    org: CurrentOrganization,
    share_service: ShareServiceDep,
    pagination: Annotated[PaginationParams, Depends()],
    mine_only: bool = Query(default=True, description="If true, only shares YOU created."),
) -> APIResponse[Page[ShareRead]]:
    created_by = current_user.id if mine_only else None
    shares, total = await share_service.list_for_organization(
        org.organization_id, created_by=created_by, limit=pagination.page_size, offset=pagination.offset
    )
    page = Page.create(
        items=[ShareRead.from_share(s) for s in shares],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )
    return APIResponse(message="Shares retrieved successfully.", data=page)


@router.delete("/{share_id}", response_model=APIResponse[None], summary="Revoke a sharing link")
async def revoke_share(
    share_id: uuid.UUID, current_user: CurrentUser, org: CurrentOrganization, share_service: ShareServiceDep
) -> APIResponse[None]:
    await share_service.revoke(current_user.id, org.organization_id, share_id)
    return APIResponse(message="Share revoked successfully.")


@router.get(
    "/redeem/{token}",
    response_model=APIResponse[ShareRead],
    summary="Look up a share by its bearer token (public, unauthenticated)",
)
async def redeem_share(
    token: str, share_service: ShareServiceDep, password: str | None = Query(default=None)
) -> APIResponse[ShareRead]:
    share = await share_service.redeem(token, password=password)
    return APIResponse(message="Share is valid.", data=ShareRead.from_share(share))


@router.get(
    "/redeem/{token}/download",
    summary="Download the shared file's bytes via a bearer token (public, unauthenticated)",
)
async def download_shared_file(
    token: str,
    file_upload_service: FileUploadServiceDep,
    share_service: ShareServiceDep,
    password: str | None = Query(default=None),
):
    share = await share_service.redeem(token, password=password)
    if share.resource_type != ResourceType.FILE:
        raise ValidationException("This share does not point at a downloadable file.")

    file = await file_upload_service.get_downloadable_in_organization(share.resource_id, share.organization_id)
    await share_service.record_access(share)

    media_type = file.mime_type or "application/octet-stream"
    disposition = f"attachment; filename=\"{file.original_filename}\"; filename*=UTF-8''{quote(file.original_filename)}"
    return StreamingResponse(
        file_upload_service.stream(file),
        media_type=media_type,
        headers={"Content-Disposition": disposition, "Content-Length": str(file.size)},
    )
