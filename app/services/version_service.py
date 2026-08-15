"""
File version-history business logic.

Phase 7: version listings are cache-aside on
`nimbusfs:file:{file_id}:versions`. Authorization is preserved exactly as
in the uncached path — the owning file is resolved through
`MetadataService`-style ownership rules *before* the cached list is
consulted, so the cached value (which contains no owner field of its own)
is never the thing granting access. Invalidated by
`CacheInvalidator.file_versions_changed`, which every version-creating
write calls.
"""

import uuid

from app.core.cache.policy import CacheEntity
from app.exceptions.custom_exceptions import FileNotFoundException
from app.models.file_version import FileVersion
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.repositories.file_version_repository import FileVersionRepository
from app.schemas.file_metadata import FileVersionRead
from app.services.cache_invalidator import CacheInvalidator
from app.services.cache_service import CacheService


class VersionService:
    def __init__(
        self,
        version_repository: FileVersionRepository,
        file_repository: FileMetadataRepository,
        *,
        cache: CacheService | None = None,
        invalidator: CacheInvalidator | None = None,
    ):
        self._versions = version_repository
        self._files = file_repository
        self._cache = cache
        self._invalidator = invalidator

    async def list_versions(self, file_id: uuid.UUID, owner_id: uuid.UUID) -> list[FileVersion]:
        file = await self._files.get_active_by_id(file_id, owner_id)
        if file is None:
            raise FileNotFoundException()
        return await self._versions.list_for_file(file_id)

    async def list_versions_cached(self, file_id: uuid.UUID, owner_id: uuid.UUID) -> list[FileVersionRead]:
        if self._cache is None or not self._cache.enabled:
            return [FileVersionRead.model_validate(v) for v in await self.list_versions(file_id, owner_id)]

        # Ownership is enforced here, against Postgres, and is NOT what is
        # being cached: the version list is only reachable once this
        # ownership-filtered lookup has already succeeded.
        file = await self._files.get_active_by_id(file_id, owner_id)
        if file is None:
            raise FileNotFoundException()

        key = self._cache.keys.file_versions(file_id)

        async def _load() -> list[dict]:
            versions = await self._versions.list_for_file(file_id)
            return [FileVersionRead.model_validate(v).model_dump(mode="json") for v in versions]

        payload = await self._cache.get_or_set(
            key, _load, self._cache.ttl_for(CacheEntity.FILE_VERSIONS), entity=CacheEntity.FILE_VERSIONS
        )
        return [FileVersionRead.model_validate(item) for item in payload]
