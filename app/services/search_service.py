"""
File search business logic — thin orchestration over the repository's
search query, plus (Phase 7) a deliberately conservative result cache.

Why search gets treated differently from every other cached entity
------------------------------------------------------------------
A search result is a *derived* view over many rows, not a single row. That
changes three things:

1. **The key must include every input that changes the result.** Query
   text, each filter, sort field and direction, page number, page size —
   and the requesting user. All of them are folded into one canonical
   fingerprint by `CacheKeyBuilder.search`, with `owner_id` kept as a
   structural key segment rather than hashed (see that module for why).
   Missing any one of them would serve a client somebody else's answer.
2. **Authorization is baked into the key, not re-checked after.** Unlike
   folders and files — where the cached payload carries `owner_id` and is
   re-authorized on read — a result set has no single owner field to
   check. So the key is *caller-scoped*: `nimbusfs:search:{owner_id}:{fp}`
   is only ever read by the user whose ID is in it, and the repository
   query underneath is already owner-filtered. This is the one entity
   type where per-user keying is the correct answer rather than a
   pessimization, and it is why the two approaches are documented
   side by side.
3. **It cannot be invalidated precisely.** Any file write can change the
   membership of an unknown number of cached queries. NimbusFS handles
   this with a coarse, bounded per-user SCAN-and-delete on every file
   mutation (`CacheInvalidator.file_changed`) plus the shortest TTL of any
   entity (90s). Fanning out precisely would require a reverse index from
   row to query, which is a search-engine feature, not a cache feature.

Size ceiling
------------
Result pages larger than `Settings.CACHE_SEARCH_MAX_ITEMS` (default 100 —
which is also `PaginationParams`'s hard `MAX_PAGE_SIZE`, so it is a real
ceiling rather than a theoretical one) are computed and returned but NOT
written to Redis. A cache is for things that are cheap to store and
frequently re-read; a large result page is neither, and one client
paginating deeply through a big corpus could otherwise evict the entire
working set of genuinely hot small keys. `CacheService.set` enforces an
independent byte ceiling (`CACHE_MAX_VALUE_BYTES`) as a second backstop.
"""

import uuid

from app.core.cache.policy import CacheEntity
from app.models.file_metadata import FileMetadata
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.schemas.file_metadata import FileMetadataRead
from app.schemas.pagination import Page
from app.schemas.search import FileSearchParams
from app.services.cache_service import CacheService


class SearchService:
    def __init__(self, file_repository: FileMetadataRepository, *, cache: CacheService | None = None):
        self._files = file_repository
        self._cache = cache

    async def search_files(
        self, owner_id: uuid.UUID, params: FileSearchParams, offset: int, limit: int
    ) -> tuple[list[FileMetadata], int]:
        return await self._files.search(owner_id, params, offset, limit)

    @staticmethod
    def _key_params(params: FileSearchParams, page: int, page_size: int) -> dict:
        """
        Every input that can change the result set, normalized.

        Normalization matters as much as completeness: `q="  Report "` and
        `q="report"` are the same search to the database (the repository
        does a case-insensitive LIKE) and must therefore be the same key,
        or the cache hit rate silently collapses on whitespace and casing.
        Datetimes are rendered ISO-8601 so two equal instants produced by
        different parses cannot key differently.
        """

        def _dt(value) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "q": params.q.strip().lower() if params.q else None,
            "folder_id": params.folder_id,
            "extension": params.extension,
            "mime_type": params.mime_type.lower() if params.mime_type else None,
            "owner_filter": params.owner_id,
            "version": params.version,
            "is_deleted": params.is_deleted,
            "created_after": _dt(params.created_after),
            "created_before": _dt(params.created_before),
            "updated_after": _dt(params.updated_after),
            "updated_before": _dt(params.updated_before),
            "sort_by": getattr(params.sort_by, "value", params.sort_by),
            "sort_order": getattr(params.sort_order, "value", params.sort_order),
            "page": page,
            "page_size": page_size,
        }

    async def search_files_page(
        self, owner_id: uuid.UUID, params: FileSearchParams, page: int, page_size: int
    ) -> Page[FileMetadataRead]:
        """
        Cache-aside search returning the fully-built `Page` the route used
        to assemble itself.

        The whole `Page` is cached (not just the rows) because `total`
        comes from a second COUNT query — which is frequently the more
        expensive half of a search — and caching only the rows would leave
        that cost on every request.
        """
        offset = (page - 1) * page_size

        if self._cache is None or not self._cache.enabled:
            items, total = await self.search_files(owner_id, params, offset, page_size)
            return Page.create(
                items=[FileMetadataRead.model_validate(f) for f in items],
                total=total,
                page=page,
                page_size=page_size,
            )

        key = self._cache.keys.search(owner_id, self._key_params(params, page, page_size))
        max_items = self._cache.policy.search_max_items

        async def _load() -> dict:
            items, total = await self.search_files(owner_id, params, offset, page_size)
            built = Page.create(
                items=[FileMetadataRead.model_validate(f) for f in items],
                total=total,
                page=page,
                page_size=page_size,
            )
            return built.model_dump(mode="json")

        def _cacheable(payload: dict) -> bool:
            return len(payload.get("items", [])) <= max_items

        payload = await self._cache.get_or_set(
            key,
            _load,
            self._cache.ttl_for(CacheEntity.SEARCH),
            entity=CacheEntity.SEARCH,
            cacheable=_cacheable,
        )
        return Page[FileMetadataRead].model_validate(payload)
