"""
Combined trash view.

`GET /folders/trash` and `GET /metadata/trash` (in their respective
routers) give entity-specific trash listings; this endpoint gives the
"everything in my trash at once" view a real Drive-like trash screen
needs, backed by `TrashService`.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.dependencies.auth import CurrentUser
from app.dependencies.providers import TrashServiceDep
from app.schemas.file_metadata import FileMetadataRead
from app.schemas.folder import FolderRead
from app.schemas.response import APIResponse

router = APIRouter(prefix="/trash", tags=["Trash"])


class TrashContents(BaseModel):
    folders: list[FolderRead]
    files: list[FileMetadataRead]


@router.get("", response_model=APIResponse[TrashContents], summary="List everything currently in the trash")
async def list_trash(current_user: CurrentUser, trash_service: TrashServiceDep) -> APIResponse[TrashContents]:
    folders, files = await trash_service.list_trash(current_user.id)
    contents = TrashContents(
        folders=[FolderRead.model_validate(f) for f in folders],
        files=[FileMetadataRead.model_validate(f) for f in files],
    )
    return APIResponse(message="Trash contents retrieved successfully.", data=contents)