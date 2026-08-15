"""
Related-key invalidation after a write (Phase 7).

The problem this solves
-----------------------
A single write rarely invalidates a single cache entry. Renaming a folder
changes: the folder's own metadata, its parent's children listing, its own
children listing (their `path` prefix moved), and the breadcrumb trail of
every descendant. If each mutating service method has to remember that
list, one of them eventually will not — and a missed invalidation is the
worst class of cache bug, because it is silent, it is user-visible, and it
persists for a full TTL.

So the fan-out lives here, once, named after the *operation* rather than
the key ("this folder moved") and the mapping from operation to key set is
in exactly one place that the tests can assert against directly.

Invalidation, not update
------------------------
Every method here DELETES. It never writes a fresh value in. Write-through
("update the cache with the new row while we are here") looks strictly
better and is a classic source of stale data under concurrency: two
concurrent writers can apply their cache updates in the opposite order to
their database commits, leaving the cache permanently disagreeing with
Postgres with no TTL-independent way to notice. Deleting is idempotent and
order-independent — the loser of any race just causes one extra read.

Timing, and the race we accept
------------------------------
Invalidation is issued inside the service method, which is inside the
request's transaction (`get_db` commits at the request boundary — see
`app/database/session.py`). So there is a window — between our DELETE and
the COMMIT — in which a concurrent reader can miss, read the *old*
committed row, and repopulate the cache with pre-write data. That entry
then survives until its TTL.

Three things bound this, and it is worth being explicit that it is bounded
rather than eliminated:
  1. The window is the remainder of one request's transaction — typically
     sub-millisecond, and never longer than the request itself.
  2. Staleness is capped by the entity TTL (5 minutes at the default), not
     unbounded.
  3. `Settings.CACHE_WRITE_GUARD_SECONDS` closes the window completely
     when a deployment needs it to (see `CacheService.invalidate`), at the
     cost of a fixed cold period after every write. Off by default.
The genuinely airtight fix — invalidating in an after-commit hook — needs a
transaction-lifecycle hook that the current per-request Unit of Work does
not expose to services. That is a real, acknowledged limitation of this
phase rather than an oversight; see docs/PHASE_7_REDIS_DESIGN.md.

Descendant breadcrumbs: precise invalidation
---------------------------------------------
Renaming or moving a folder changes the materialized `path` of every
descendant, and therefore the correctness of every descendant's breadcrumb
cache. `FolderRepository.list_descendants()` already exists (it's how
soft-delete cascades), so the folder service passes the descendant ID list
it already has to `descendant_breadcrumbs_changed()` below, which deletes
each descendant's exact `breadcrumbs` key — no SCAN, no subtree walk of our
own, no new Redis secondary index. This is O(descendant count) exact
deletes, the same shape `cascade_rename` already pays in Postgres for the
same rename/move, so it adds no new query complexity class.

`folder_moved()`/`folder_changed()` by themselves still only clear the
folder's own keys and its parents' children listings — callers that also
mutate a subtree (`rename_folder`, `move_folder`) MUST call
`descendant_breadcrumbs_changed()` too, using descendant IDs captured
*before* `cascade_rename` runs (IDs are stable across a path rewrite; only
the `path` column changes). Forgetting this on a future subtree-mutating
operation reintroduces the staleness this section used to describe as
accepted — so it no longer is.
"""

from __future__ import annotations

import uuid

from app.logging.logger import get_logger
from app.services.cache_service import CacheService

logger = get_logger(__name__)


class CacheInvalidator:
    """
    Operation-named invalidation fan-out. Thin by design — it owns the
    key *sets*, `CacheService` owns talking to Redis.
    """

    def __init__(self, cache: CacheService):
        self._cache = cache
        self._keys = cache.keys

    @property
    def cache(self) -> CacheService:
        return self._cache

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------
    async def user_changed(self, user_id: uuid.UUID) -> None:
        await self._invalidate("user_changed", self._keys.user(user_id))

    # ------------------------------------------------------------------
    # Folders
    # ------------------------------------------------------------------
    async def folder_changed(
        self,
        folder_id: uuid.UUID,
        owner_id: uuid.UUID,
        parent_folder_id: uuid.UUID | None,
    ) -> None:
        """
        Create / rename / trash / restore / permanent-delete of one folder.

        Clears the folder's own key plus every key derived from it (its
        children listings under all sort/filter variants, its breadcrumbs)
        via a bounded SCAN on `folder:<id>*`, and the parent's children
        listings so the folder appears/disappears from its parent's view.
        """
        await self._invalidate_patterns(
            "folder_changed",
            self._keys.folder_pattern(folder_id),
            self._keys.folder_children_pattern(parent_folder_id, owner_id),
        )

    async def folder_moved(
        self,
        folder_id: uuid.UUID,
        owner_id: uuid.UUID,
        old_parent_folder_id: uuid.UUID | None,
        new_parent_folder_id: uuid.UUID | None,
    ) -> None:
        """Move/rename: both the source and destination parents' listings change."""
        patterns = [
            self._keys.folder_pattern(folder_id),
            self._keys.folder_children_pattern(old_parent_folder_id, owner_id),
        ]
        if new_parent_folder_id != old_parent_folder_id:
            patterns.append(self._keys.folder_children_pattern(new_parent_folder_id, owner_id))
        await self._invalidate_patterns("folder_moved", *patterns)

    async def descendant_breadcrumbs_changed(self, descendant_ids: list[uuid.UUID]) -> None:
        """
        Precisely invalidates the breadcrumb cache of every folder in
        `descendant_ids` after an ancestor rename/move changed their path.

        Callers must gather `descendant_ids` (e.g. via
        `FolderRepository.list_descendants`) BEFORE `cascade_rename`
        mutates paths — folder IDs are stable across the rewrite, so the
        set gathered pre-mutation is still exactly the right set.
        A no-op for a leaf folder (empty list) or when caching is off.
        """
        if not descendant_ids:
            return
        keys = [self._keys.folder_breadcrumbs(folder_id) for folder_id in descendant_ids]
        await self._invalidate("descendant_breadcrumbs_changed", *keys)

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------
    async def file_changed(
        self,
        file_id: uuid.UUID,
        owner_id: uuid.UUID,
        folder_id: uuid.UUID | None,
        *,
        also_search: bool = True,
    ) -> None:
        """
        Create / update / rename / trash / restore / permanent-delete /
        new-version of one file.

        Also drops that owner's cached search pages by default: a search
        result set is a derived view whose membership any file write can
        change, and there is no way to know which cached queries matched
        without re-running them. A per-user SCAN is bounded and cheap
        relative to the correctness it buys (a just-uploaded file showing
        up in search immediately, rather than up to 90 seconds later).
        """
        patterns = [
            self._keys.file_pattern(file_id),
            self._keys.folder_children_pattern(folder_id, owner_id),
        ]
        if also_search:
            patterns.append(self._keys.search_pattern(owner_id))
        await self._invalidate_patterns("file_changed", *patterns)

    async def file_moved(
        self,
        file_id: uuid.UUID,
        owner_id: uuid.UUID,
        old_folder_id: uuid.UUID | None,
        new_folder_id: uuid.UUID | None,
    ) -> None:
        patterns = [
            self._keys.file_pattern(file_id),
            self._keys.folder_children_pattern(old_folder_id, owner_id),
            self._keys.search_pattern(owner_id),
        ]
        if new_folder_id != old_folder_id:
            patterns.append(self._keys.folder_children_pattern(new_folder_id, owner_id))
        await self._invalidate_patterns("file_moved", *patterns)

    async def file_versions_changed(self, file_id: uuid.UUID) -> None:
        await self._invalidate("file_versions_changed", self._keys.file_versions(file_id))

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    async def search_changed(self, owner_id: uuid.UUID) -> None:
        await self._invalidate_patterns("search_changed", self._keys.search_pattern(owner_id))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _invalidate(self, operation: str, *keys: str) -> None:
        removed = await self._cache.invalidate(*keys)
        logger.info(
            "cache_invalidation",
            operation=operation,
            mode="exact",
            key_count=len(keys),
            removed=removed,
            result="ok",
        )

    async def _invalidate_patterns(self, operation: str, *patterns: str) -> None:
        if not self._cache.enabled:
            return
        removed = 0
        for pattern in patterns:
            removed += await self._cache.invalidate_pattern(pattern)
        logger.info(
            "cache_invalidation",
            operation=operation,
            mode="pattern",
            pattern_count=len(patterns),
            removed=removed,
            result="ok",
        )
