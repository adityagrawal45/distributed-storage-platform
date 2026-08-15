"""
File metadata routes.

No actual file bytes are handled anywhere in this router — every endpoint
here operates purely on metadata rows, per Phase 2's scope.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.rate_limiter import RateLimitCategory
from app.dependencies.auth import CurrentUser
from app.dependencies.providers import MetadataServiceDep, SearchServiceDep, VersionServiceDep
from app.dependencies.rate_limit import rate_limit
from app.schemas.file_metadata import (
    FileMetadataCreate,
    FileMetadataRead,
    FileMetadataUpdate,
    FileMove,
    FileRename,
    FileVersionRead,
)
from app.schemas.pagination import Page, PaginationParams
from app.schemas.response import APIResponse
from app.schemas.search import FileSearchParams

# Phase 7: router-level METADATA budget (see folders/routes.py for the
# rationale). `/metadata/search` additionally carries the tighter SEARCH
# budget — search is materially more expensive per call than a keyed read,
# so it gets its own bucket on top of the shared one rather than
# competing for the same tokens.
router = APIRouter(
    prefix="/metadata",
    tags=["File Metadata"],
    dependencies=[Depends(rate_limit(RateLimitCategory.METADATA))],
)


@router.post(
    "", response_model=APIResponse[FileMetadataRead], status_code=status.HTTP_201_CREATED,
    summary="Create file metadata (reserves a storage slot; no bytes uploaded)",
)
async def create_metadata(
    payload: FileMetadataCreate, current_user: CurrentUser, metadata_service: MetadataServiceDep
) -> APIResponse[FileMetadataRead]:
    file = await metadata_service.create_metadata(current_user.id, payload)
    return APIResponse(message="File metadata created successfully.", data=FileMetadataRead.model_validate(file))


@router.get(
    "/search",
    response_model=APIResponse[Page[FileMetadataRead]],
    summary="Search files",
    dependencies=[Depends(rate_limit(RateLimitCategory.SEARCH))],
)
async def search_files(
    current_user: CurrentUser,
    search_service: SearchServiceDep,
    search_params: Annotated[FileSearchParams, Depends()],
    pagination: Annotated[PaginationParams, Depends()],
) -> APIResponse[Page[FileMetadataRead]]:
    page = await search_service.search_files_page(
        current_user.id, search_params, pagination.page, pagination.page_size
    )
    return APIResponse(message="Search completed successfully.", data=page)


@router.get("/trash", response_model=APIResponse[list[FileMetadataRead]], summary="List files currently in the trash")
async def list_file_trash(
    current_user: CurrentUser, metadata_service: MetadataServiceDep
) -> APIResponse[list[FileMetadataRead]]:
    files = await metadata_service.list_trash(current_user.id)
    return APIResponse(
        message="Trashed files retrieved successfully.", data=[FileMetadataRead.model_validate(f) for f in files]
    )


@router.get("/{file_id}", response_model=APIResponse[FileMetadataRead], summary="Get file metadata by ID")
async def get_metadata(
    file_id: uuid.UUID, current_user: CurrentUser, metadata_service: MetadataServiceDep
) -> APIResponse[FileMetadataRead]:
    file = await metadata_service.get_metadata_cached(file_id, current_user.id)
    return APIResponse(message="File metadata retrieved successfully.", data=file)


@router.put("/{file_id}", response_model=APIResponse[FileMetadataRead], summary="Update file metadata")
async def update_metadata(
    file_id: uuid.UUID,
    payload: FileMetadataUpdate,
    current_user: CurrentUser,
    metadata_service: MetadataServiceDep,
) -> APIResponse[FileMetadataRead]:
    file = await metadata_service.update_metadata(file_id, current_user.id, payload, current_user.id)
    return APIResponse(message="File metadata updated successfully.", data=FileMetadataRead.model_validate(file))


@router.post("/{file_id}/rename", response_model=APIResponse[FileMetadataRead], summary="Rename a file")
async def rename_file(
    file_id: uuid.UUID, payload: FileRename, current_user: CurrentUser, metadata_service: MetadataServiceDep
) -> APIResponse[FileMetadataRead]:
    file = await metadata_service.rename_file(file_id, current_user.id, payload.name, current_user.id)
    return APIResponse(message="File renamed successfully.", data=FileMetadataRead.model_validate(file))


@router.post("/{file_id}/move", response_model=APIResponse[FileMetadataRead], summary="Move a file to a new folder")
async def move_file(
    file_id: uuid.UUID, payload: FileMove, current_user: CurrentUser, metadata_service: MetadataServiceDep
) -> APIResponse[FileMetadataRead]:
    file = await metadata_service.move_file(file_id, current_user.id, payload.new_folder_id, current_user.id)
    return APIResponse(message="File moved successfully.", data=FileMetadataRead.model_validate(file))


@router.delete("/{file_id}", response_model=APIResponse[None], summary="Move a file to the trash (soft delete)")
async def delete_file(
    file_id: uuid.UUID, current_user: CurrentUser, metadata_service: MetadataServiceDep
) -> APIResponse[None]:
    await metadata_service.delete_file(file_id, current_user.id, current_user.id)
    return APIResponse(message="File moved to trash successfully.")


@router.post("/{file_id}/restore", response_model=APIResponse[FileMetadataRead], summary="Restore a file from the trash")
async def restore_file(
    file_id: uuid.UUID, current_user: CurrentUser, metadata_service: MetadataServiceDep
) -> APIResponse[FileMetadataRead]:
    file = await metadata_service.restore_file(file_id, current_user.id, current_user.id)
    return APIResponse(message="File restored successfully.", data=FileMetadataRead.model_validate(file))


@router.delete(
    "/{file_id}/permanent",
    response_model=APIResponse[None],
    summary="Permanently delete a trashed file's metadata (irreversible)",
)
async def permanent_delete_file(
    file_id: uuid.UUID, current_user: CurrentUser, metadata_service: MetadataServiceDep
) -> APIResponse[None]:
    await metadata_service.permanent_delete_file(file_id, current_user.id)
    return APIResponse(message="File permanently deleted.")


@router.get(
    "/{file_id}/versions", response_model=APIResponse[list[FileVersionRead]], summary="Get a file's version history"
)
async def get_file_versions(
    file_id: uuid.UUID, current_user: CurrentUser, version_service: VersionServiceDep
) -> APIResponse[list[FileVersionRead]]:
    versions = await version_service.list_versions_cached(file_id, current_user.id)
    return APIResponse(message="Version history retrieved successfully.", data=versions)