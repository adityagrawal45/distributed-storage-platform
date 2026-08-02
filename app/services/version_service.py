"""File version-history business logic."""

import uuid

from app.exceptions.custom_exceptions import FileNotFoundException
from app.models.file_version import FileVersion
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.repositories.file_version_repository import FileVersionRepository


class VersionService:
    def __init__(self, version_repository: FileVersionRepository, file_repository: FileMetadataRepository):
        self._versions = version_repository
        self._files = file_repository

    async def list_versions(self, file_id: uuid.UUID, owner_id: uuid.UUID) -> list[FileVersion]:
        file = await self._files.get_active_by_id(file_id, owner_id)
        if file is None:
            raise FileNotFoundException()
        return await self._versions.list_for_file(file_id)