"""
File metadata business logic.

Design decision: `stored_filename` generation. When the client doesn't
supply one, we generate `{uuid4}{extension}` — a name that's guaranteed
globally unique, filesystem/object-store-safe (no user-controlled
characters), and stable for the file's entire lifetime even if the user
later renames `original_filename`. This is exactly the "reserve the
future object name" behavior Phase 2 requires without touching storage.
"""

import uuid
from datetime import datetime, timezone

from app.exceptions.custom_exceptions import (
    DuplicateFileException,
    FileNotFoundException,
    FolderNotFoundException,
    ValidationException,
)
from app.models.file_metadata import FileMetadata
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.repositories.file_version_repository import FileVersionRepository
from app.repositories.folder_repository import FolderRepository
from app.schemas.file_metadata import FileMetadataCreate, FileMetadataUpdate


class MetadataService:
    def __init__(
        self,
        file_repository: FileMetadataRepository,
        version_repository: FileVersionRepository,
        folder_repository: FolderRepository,
    ):
        self._files = file_repository
        self._versions = version_repository
        self._folders = folder_repository

    async def _get_owned_active(self, file_id: uuid.UUID, owner_id: uuid.UUID) -> FileMetadata:
        file = await self._files.get_active_by_id(file_id, owner_id)
        if file is None:
            raise FileNotFoundException()
        return file

    async def _validate_folder(self, owner_id: uuid.UUID, folder_id: uuid.UUID | None) -> None:
        if folder_id is None:
            return
        folder = await self._folders.get_active_by_id(folder_id, owner_id)
        if folder is None:
            raise FolderNotFoundException(detail="Target folder not found.")

    @staticmethod
    def _split_extension(filename: str) -> str | None:
        if "." not in filename:
            return None
        return filename.rsplit(".", 1)[-1].lower()

    async def create_metadata(self, owner_id: uuid.UUID, payload: FileMetadataCreate) -> FileMetadata:
        await self._validate_folder(owner_id, payload.folder_id)

        if await self._files.name_exists_in_folder(owner_id, payload.folder_id, payload.original_filename):
            raise DuplicateFileException()

        extension = self._split_extension(payload.original_filename)
        stored_filename = payload.stored_filename or (
            f"{uuid.uuid4()}.{extension}" if extension else str(uuid.uuid4())
        )

        file = FileMetadata(
            owner_id=owner_id,
            folder_id=payload.folder_id,
            original_filename=payload.original_filename,
            stored_filename=stored_filename,
            extension=extension,
            mime_type=payload.mime_type,
            size=payload.size,
            checksum=payload.checksum,
            version=1,
            created_by=owner_id,
            updated_by=owner_id,
        )
        file = await self._files.add(file)
        await self._versions.create(file_id=file.id, version=1, checksum=file.checksum, size=file.size)
        return file

    async def get_metadata(self, file_id: uuid.UUID, owner_id: uuid.UUID) -> FileMetadata:
        return await self._get_owned_active(file_id, owner_id)

    async def update_metadata(
        self, file_id: uuid.UUID, owner_id: uuid.UUID, payload: FileMetadataUpdate, actor_id: uuid.UUID
    ) -> FileMetadata:
        """
        Updates mutable metadata fields. If `size` or `checksum` change,
        this is treated as a new version: `version` increments and a new
        `FileVersion` snapshot row is recorded, mirroring how a real
        upload-a-new-revision flow will work once storage is wired up.
        """
        file = await self._get_owned_active(file_id, owner_id)

        content_changed = False
        if payload.mime_type is not None:
            file.mime_type = payload.mime_type
        if payload.size is not None and payload.size != file.size:
            file.size = payload.size
            content_changed = True
        if payload.checksum is not None and payload.checksum != file.checksum:
            file.checksum = payload.checksum
            content_changed = True

        file.updated_by = actor_id

        if content_changed:
            file.version += 1
            await self._versions.create(
                file_id=file.id, version=file.version, checksum=file.checksum, size=file.size
            )

        return file

    async def rename_file(
        self, file_id: uuid.UUID, owner_id: uuid.UUID, new_name: str, actor_id: uuid.UUID
    ) -> FileMetadata:
        file = await self._get_owned_active(file_id, owner_id)

        if file.original_filename == new_name:
            return file

        if await self._files.name_exists_in_folder(owner_id, file.folder_id, new_name, exclude_id=file.id):
            raise DuplicateFileException()

        file.original_filename = new_name
        file.extension = self._split_extension(new_name)
        file.updated_by = actor_id
        return file

    async def move_file(
        self, file_id: uuid.UUID, owner_id: uuid.UUID, new_folder_id: uuid.UUID | None, actor_id: uuid.UUID
    ) -> FileMetadata:
        file = await self._get_owned_active(file_id, owner_id)

        if file.folder_id == new_folder_id:
            return file

        await self._validate_folder(owner_id, new_folder_id)

        if await self._files.name_exists_in_folder(
            owner_id, new_folder_id, file.original_filename, exclude_id=file.id
        ):
            raise DuplicateFileException(detail="A file with this name already exists in the destination.")

        file.folder_id = new_folder_id
        file.updated_by = actor_id
        return file

    async def delete_file(self, file_id: uuid.UUID, owner_id: uuid.UUID, actor_id: uuid.UUID) -> None:
        file = await self._get_owned_active(file_id, owner_id)
        file.is_deleted = True
        file.deleted_at = datetime.now(timezone.utc)
        file.deleted_by = actor_id
        file.updated_by = actor_id

    async def restore_file(self, file_id: uuid.UUID, owner_id: uuid.UUID, actor_id: uuid.UUID) -> FileMetadata:
        file = await self._files.get_any_by_id(file_id, owner_id)
        if file is None or not file.is_deleted:
            raise FileNotFoundException(detail="File not found in trash.")

        file.is_deleted = False
        file.deleted_at = None
        file.deleted_by = None
        file.updated_by = actor_id
        return file

    async def permanent_delete_file(self, file_id: uuid.UUID, owner_id: uuid.UUID) -> None:
        file = await self._files.get_any_by_id(file_id, owner_id)
        if file is None:
            raise FileNotFoundException()
        if not file.is_deleted:
            raise ValidationException(detail="File must be moved to trash before it can be permanently deleted.")
        # FileVersion rows cascade-delete via their FK's ON DELETE CASCADE.
        await self._files.delete(file)

    async def list_trash(self, owner_id: uuid.UUID) -> list[FileMetadata]:
        return await self._files.list_trash(owner_id)