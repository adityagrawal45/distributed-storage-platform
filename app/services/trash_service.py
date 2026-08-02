"""
Trash business logic.

Design decision: there is deliberately no separate `Trash` table (see the
`SoftDeleteMixin` docstring for the full rationale). `TrashService` is
simply a read-side aggregator: it asks the folder and file repositories
for everything currently flagged `is_deleted=True` for the owner and
returns them together. The actual restore/permanent-delete *operations*
live on `FolderService`/`MetadataService` (they already own the
entity-specific validation for those actions) — this service only owns
the combined "what's in my trash" view.
"""

import uuid

from app.models.file_metadata import FileMetadata
from app.models.folder import Folder
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.repositories.folder_repository import FolderRepository


class TrashService:
    def __init__(self, folder_repository: FolderRepository, file_repository: FileMetadataRepository):
        self._folders = folder_repository
        self._files = file_repository

    async def list_trash(self, owner_id: uuid.UUID) -> tuple[list[Folder], list[FileMetadata]]:
        folders = await self._folders.list_trash(owner_id)
        files = await self._files.list_trash(owner_id)
        return folders, files