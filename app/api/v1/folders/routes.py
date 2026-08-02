"""
Folder routes.

Design decision: routes stay thin — parse/validate input via Pydantic
and query params, delegate everything to `FolderService`, wrap the
result in `APIResponse`. All hierarchy rules (path/level bookkeeping,
duplicate-name checks, circular-move prevention) live in the service.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.auth import CurrentUser
from app.dependencies.providers import FolderServiceDep
from app.schemas.folder import (
    BreadcrumbItem,
    BreadcrumbResponse,
    FolderCreate,
    FolderMove,
    FolderRead,
    FolderRename,
    FolderTreeNode,
)
from app.schemas.response import APIResponse
from app.schemas.search import FolderListParams

router = APIRouter(prefix="/folders", tags=["Folders"])


@router.post("", response_model=APIResponse[FolderRead], status_code=status.HTTP_201_CREATED, summary="Create a folder")
async def create_folder(
    payload: FolderCreate, current_user: CurrentUser, folder_service: FolderServiceDep
) -> APIResponse[FolderRead]:
    folder = await folder_service.create_folder(current_user.id, payload.name, payload.parent_folder_id)
    return APIResponse(message="Folder created successfully.", data=FolderRead.model_validate(folder))


@router.get("", response_model=APIResponse[list[FolderRead]], summary="List folder contents (child folders)")
async def list_folders(
    current_user: CurrentUser,
    folder_service: FolderServiceDep,
    params: Annotated[FolderListParams, Depends()],
    parent_folder_id: uuid.UUID | None = Query(default=None, description="Null lists top-level folders."),
) -> APIResponse[list[FolderRead]]:
    folders = await folder_service.list_children(current_user.id, parent_folder_id, params)
    return APIResponse(
        message="Folders retrieved successfully.", data=[FolderRead.model_validate(f) for f in folders]
    )


@router.get("/tree", response_model=APIResponse[list[FolderTreeNode]], summary="Get the folder tree")
async def get_folder_tree(
    current_user: CurrentUser,
    folder_service: FolderServiceDep,
    root_folder_id: uuid.UUID | None = Query(
        default=None, description="Null returns a forest of all top-level folders."
    ),
) -> APIResponse[list[FolderTreeNode]]:
    tree = await folder_service.get_tree(current_user.id, root_folder_id)
    return APIResponse(message="Folder tree retrieved successfully.", data=tree)


@router.get("/breadcrumb", response_model=APIResponse[BreadcrumbResponse], summary="Get breadcrumb navigation for a folder")
async def get_breadcrumb(
    current_user: CurrentUser,
    folder_service: FolderServiceDep,
    folder_id: uuid.UUID = Query(..., description="The folder to build a breadcrumb trail for."),
) -> APIResponse[BreadcrumbResponse]:
    items = await folder_service.get_breadcrumb(folder_id, current_user.id)
    return APIResponse(message="Breadcrumb retrieved successfully.", data=BreadcrumbResponse(items=items))


@router.get("/trash", response_model=APIResponse[list[FolderRead]], summary="List folders currently in the trash")
async def list_folder_trash(
    current_user: CurrentUser, folder_service: FolderServiceDep
) -> APIResponse[list[FolderRead]]:
    folders = await folder_service.list_trash(current_user.id)
    return APIResponse(
        message="Trashed folders retrieved successfully.", data=[FolderRead.model_validate(f) for f in folders]
    )


@router.get("/{folder_id}", response_model=APIResponse[FolderRead], summary="Get a folder by ID")
async def get_folder(
    folder_id: uuid.UUID, current_user: CurrentUser, folder_service: FolderServiceDep
) -> APIResponse[FolderRead]:
    folder = await folder_service.get_folder(folder_id, current_user.id)
    return APIResponse(message="Folder retrieved successfully.", data=FolderRead.model_validate(folder))


@router.put("/{folder_id}", response_model=APIResponse[FolderRead], summary="Rename a folder")
async def rename_folder(
    folder_id: uuid.UUID, payload: FolderRename, current_user: CurrentUser, folder_service: FolderServiceDep
) -> APIResponse[FolderRead]:
    folder = await folder_service.rename_folder(folder_id, current_user.id, payload.name, current_user.id)
    return APIResponse(message="Folder renamed successfully.", data=FolderRead.model_validate(folder))


@router.post("/{folder_id}/move", response_model=APIResponse[FolderRead], summary="Move a folder to a new parent")
async def move_folder(
    folder_id: uuid.UUID, payload: FolderMove, current_user: CurrentUser, folder_service: FolderServiceDep
) -> APIResponse[FolderRead]:
    folder = await folder_service.move_folder(
        folder_id, current_user.id, payload.new_parent_folder_id, current_user.id
    )
    return APIResponse(message="Folder moved successfully.", data=FolderRead.model_validate(folder))


@router.delete("/{folder_id}", response_model=APIResponse[None], summary="Move a folder to the trash (soft delete)")
async def delete_folder(
    folder_id: uuid.UUID, current_user: CurrentUser, folder_service: FolderServiceDep
) -> APIResponse[None]:
    await folder_service.delete_folder(folder_id, current_user.id, current_user.id)
    return APIResponse(message="Folder moved to trash successfully.")


@router.post("/{folder_id}/restore", response_model=APIResponse[FolderRead], summary="Restore a folder from the trash")
async def restore_folder(
    folder_id: uuid.UUID, current_user: CurrentUser, folder_service: FolderServiceDep
) -> APIResponse[FolderRead]:
    folder = await folder_service.restore_folder(folder_id, current_user.id, current_user.id)
    return APIResponse(message="Folder restored successfully.", data=FolderRead.model_validate(folder))


@router.delete(
    "/{folder_id}/permanent",
    response_model=APIResponse[None],
    summary="Permanently delete a trashed folder (irreversible)",
)
async def permanent_delete_folder(
    folder_id: uuid.UUID, current_user: CurrentUser, folder_service: FolderServiceDep
) -> APIResponse[None]:
    await folder_service.permanent_delete_folder(folder_id, current_user.id)
    return APIResponse(message="Folder permanently deleted.")