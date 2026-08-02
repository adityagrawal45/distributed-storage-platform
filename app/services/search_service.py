"""File search business logic — thin orchestration over the repository's search query."""

import uuid

from app.models.file_metadata import FileMetadata
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.schemas.search import FileSearchParams


class SearchService:
    def __init__(self, file_repository: FileMetadataRepository):
        self._files = file_repository

    async def search_files(
        self, owner_id: uuid.UUID, params: FileSearchParams, offset: int, limit: int
    ) -> tuple[list[FileMetadata], int]:
        return await self._files.search(owner_id, params, offset, limit)