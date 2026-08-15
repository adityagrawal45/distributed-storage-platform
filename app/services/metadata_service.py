"""
File metadata business logic.

Design decision: `stored_filename` generation. When the client doesn't
supply one, we generate `{uuid4}{extension}` — a name that's guaranteed
globally unique, filesystem/object-store-safe (no user-controlled
characters), and stable for the file's entire lifetime even if the user
later renames `original_filename`. This is exactly the "reserve the
future object name" behavior Phase 2 requires without touching storage.

Phase 7 - caching and authorization
-----------------------------------
`get_metadata_cached` is cache-aside on `nimbusfs:file:{id}`, a
resource-scoped key. As with folders, this does NOT bypass the ownership
filter that `FileMetadataRepository.get_active_by_id`'s WHERE clause
provides: `_authorize_cached` re-applies "owned by this caller AND not
soft-deleted" to the cached payload and raises the identical
`FileNotFoundException` on mismatch, so a non-owner cannot use the cache
to confirm that a file ID exists. There is no per-user variation in a
file's representation today (NimbusFS has no sharing yet - see README's
future roadmap), so a per-resource key is correct; if per-user
*permissions* are added in a later phase, the permission set must become
part of the key or be re-evaluated on every read, and this docstring is
the place that will need updating.

Every mutating method invalidates through `CacheInvalidator.file_changed`
/ `file_moved`, which also clears the containing folder's children
listings and the owner's cached search pages.
"""

import uuid
from datetime import datetime, timezone

from app.core.cache.policy import CacheEntity
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
from app.schemas.file_metadata import FileMetadataCreate, FileMetadataRead, FileMetadataUpdate
from app.services.cache_invalidator import CacheInvalidator
from app.services.cache_service import CacheService


class MetadataService:
    def __init__(
        self,
        file_repository: FileMetadataRepository,
        version_repository: FileVersionRepository,
        folder_repository: FolderRepository,
        *,
        cache: CacheService | None = None,
        invalidator: CacheInvalidator | None = None,
    ):
        self._files = file_repository
        self._versions = version_repository
        self._folders = folder_repository
        self._cache = cache
        self._invalidator = invalidator

    # ------------------------------------------------------------------
    # Cache helpers (Phase 7)
    # ------------------------------------------------------------------
    @property
    def _caching(self) -> bool:
        return self._cache is not None and self._cache.enabled

    @staticmethod
    def _authorize_cached(payload: dict, owner_id: uuid.UUID) -> FileMetadataRead:
        """Re-applies the repository ownership/not-deleted filter to a cached payload."""
        file = FileMetadataRead.model_validate(payload)
        if str(file.owner_id) != str(owner_id) or file.is_deleted:
            raise FileNotFoundException()
        return file

    async def _invalidate_file(self, file: FileMetadata) -> None:
        if self._invalidator is not None:
            await self._invalidator.file_changed(file.id, file.owner_id, file.folder_id)

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
        await self._invalidate_file(file)
        return file

    async def get_metadata(self, file_id: uuid.UUID, owner_id: uuid.UUID) -> FileMetadata:
        return await self._get_owned_active(file_id, owner_id)

    async def get_metadata_cached(self, file_id: uuid.UUID, owner_id: uuid.UUID) -> FileMetadataRead:
        """Cache-aside `get_metadata`, returning the API schema. See module docstring on authorization."""
        if not self._caching:
            return FileMetadataRead.model_validate(await self._get_owned_active(file_id, owner_id))

        key = self._cache.keys.file(file_id)

        async def _load() -> dict:
            # Loaded un-owner-filtered on purpose: the cached entry is a
            # per-resource representation and `_authorize_cached` applies
            # the ownership check to the cached and fresh paths alike.
            file = await self._files.get_any_by_id(file_id, owner_id)
            if file is None:
                raise FileNotFoundException()
            return FileMetadataRead.model_validate(file).model_dump(mode="json")

        payload = await self._cache.get_or_set(
            key, _load, self._cache.ttl_for(CacheEntity.FILE), entity=CacheEntity.FILE
        )
        return self._authorize_cached(payload, owner_id)

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

        await self._invalidate_file(file)
        if content_changed and self._invalidator is not None:
            await self._invalidator.file_versions_changed(file.id)
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
        await self._invalidate_file(file)
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

        old_folder_id = file.folder_id
        file.folder_id = new_folder_id
        file.updated_by = actor_id
        if self._invalidator is not None:
            await self._invalidator.file_moved(file.id, file.owner_id, old_folder_id, new_folder_id)
        return file

    async def delete_file(self, file_id: uuid.UUID, owner_id: uuid.UUID, actor_id: uuid.UUID) -> None:
        file = await self._get_owned_active(file_id, owner_id)
        file.is_deleted = True
        file.deleted_at = datetime.now(timezone.utc)
        file.deleted_by = actor_id
        file.updated_by = actor_id
        await self._invalidate_file(file)

    async def restore_file(self, file_id: uuid.UUID, owner_id: uuid.UUID, actor_id: uuid.UUID) -> FileMetadata:
        file = await self._files.get_any_by_id(file_id, owner_id)
        if file is None or not file.is_deleted:
            raise FileNotFoundException(detail="File not found in trash.")

        file.is_deleted = False
        file.deleted_at = None
        file.deleted_by = None
        file.updated_by = actor_id
        await self._invalidate_file(file)
        return file

    async def permanent_delete_file(self, file_id: uuid.UUID, owner_id: uuid.UUID) -> None:
        file = await self._files.get_any_by_id(file_id, owner_id)
        if file is None:
            raise FileNotFoundException()
        if not file.is_deleted:
            raise ValidationException(detail="File must be moved to trash before it can be permanently deleted.")
        # FileVersion rows cascade-delete via their FK's ON DELETE CASCADE.
        await self._files.delete(file)
        await self._invalidate_file(file)

    async def list_trash(self, owner_id: uuid.UUID) -> list[FileMetadata]:
        return await self._files.list_trash(owner_id)