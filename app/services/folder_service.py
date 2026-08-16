"""
Folder business logic.

Design decisions:
- Every mutation re-validates ownership by always querying with
  `owner_id` in the WHERE clause (via the repository) — a user can never
  even discover whether another user's folder ID exists, let alone
  modify it. This is enforced at the repository layer, not just checked
  after the fact in the service, so there's no path that accidentally
  skips it.
- Move validation order matters: we check "does target exist" before
  "is target a descendant of self" before "is target the same as
  current parent" — cheapest/most-common failure reasons first.
- Path/level recomputation follows the same formula in both `create_folder`
  and `move_folder` (`path = parent.path + '/' + name`, `level = parent.level
  + 1`), keeping the materialized-path invariant consistent everywhere a
  folder's position in the tree can change.

Phase 7 — caching, and how authorization survives it
----------------------------------------------------
Three read paths are cache-aside: folder metadata, a folder's children
listing, and a folder's breadcrumb trail. The authorization story for each
is different, so it is spelled out rather than assumed:

- **Folder metadata** (`nimbusfs:folder:{id}`) is keyed by resource, not by
  caller — one folder has one representation. Ownership is NOT skipped: the
  cached payload carries `owner_id`, and `_authorize_cached` re-applies
  exactly the check the repository's `WHERE owner_id = :owner` clause would
  have applied, raising the same `FolderNotFoundException` (never a 403 —
  a user must not be able to probe for the existence of another user's
  folder IDs, which is the property the repository-level filter provides
  and which the cache must not weaken). The same check rejects a
  soft-deleted folder, matching `get_active_by_id`'s semantics.
- **Children listings** are keyed per folder *and* per listing parameter
  set, and are only reachable after `get_folder_cached` has already
  authorized the parent — so a non-owner never gets as far as the listing
  key. Root-level listings (`parent_folder_id is None`) have no parent
  folder to authorize against, so their key is owner-scoped instead.
- **Breadcrumbs** likewise authorize the target folder first; the trail
  itself contains only ancestors of an already-authorized folder.

Nothing here caches an *authorization decision*. It caches resources, and
every read re-derives the decision from the cached resource's owner.

Invalidation happens in every mutating method via `CacheInvalidator` (see
that module for the fan-out and the acknowledged
invalidate-before-commit race).
"""

import uuid
from datetime import datetime, timezone

from app.core.cache.policy import CacheEntity
from app.events.emitter import OutboxEmitterMixin
from app.events.envelope import EventType
from app.exceptions.custom_exceptions import (
    CircularReferenceException,
    DuplicateFolderException,
    FolderNotFoundException,
    ValidationException,
)
from app.models.folder import Folder
from app.repositories.folder_repository import FolderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.folder import BreadcrumbItem, FolderRead, FolderTreeNode
from app.schemas.search import FolderListParams
from app.services.cache_invalidator import CacheInvalidator
from app.services.cache_service import CacheService
from app.utils.path_utils import build_child_path, is_same_or_descendant_path


class FolderService(OutboxEmitterMixin):
    def __init__(
        self,
        folder_repository: FolderRepository,
        *,
        cache: CacheService | None = None,
        invalidator: CacheInvalidator | None = None,
        outbox: OutboxRepository | None = None,
    ):
        self._folders = folder_repository
        self._cache = cache
        self._invalidator = invalidator
        # Phase 8: keyword-only, None-default — unset means no events, so
        # every pre-existing construction keeps working unchanged.
        self._outbox = outbox

    # ------------------------------------------------------------------
    # Cache helpers (Phase 7)
    # ------------------------------------------------------------------
    @property
    def _caching(self) -> bool:
        return self._cache is not None and self._cache.enabled

    @staticmethod
    def _authorize_cached(payload: dict, owner_id: uuid.UUID) -> FolderRead:
        """
        Re-applies the repository's ownership + not-deleted filter to a
        value that came from the cache instead of from a WHERE clause.

        Raises `FolderNotFoundException` (not an authorization error) on
        mismatch, preserving the existing "you cannot even discover
        another user's folder IDs" property.
        """
        folder = FolderRead.model_validate(payload)
        if str(folder.owner_id) != str(owner_id) or folder.is_deleted:
            raise FolderNotFoundException()
        return folder

    @staticmethod
    def _listing_params(params: FolderListParams) -> dict:
        """The parameters that make two children listings differ, for key derivation."""
        return {
            "is_deleted": params.is_deleted,
            "sort_by": getattr(params.sort_by, "value", params.sort_by),
            "sort_order": getattr(params.sort_order, "value", params.sort_order),
        }

    async def _invalidate_folder(self, folder: Folder) -> None:
        if self._invalidator is not None:
            await self._invalidator.folder_changed(folder.id, folder.owner_id, folder.parent_folder_id)

    async def _get_owned_active(self, folder_id: uuid.UUID, owner_id: uuid.UUID) -> Folder:
        folder = await self._folders.get_active_by_id(folder_id, owner_id)
        if folder is None:
            raise FolderNotFoundException()
        return folder

    async def _resolve_parent(self, owner_id: uuid.UUID, parent_folder_id: uuid.UUID | None) -> Folder | None:
        if parent_folder_id is None:
            return None
        parent = await self._folders.get_active_by_id(parent_folder_id, owner_id)
        if parent is None:
            raise FolderNotFoundException(detail="Target parent folder not found.")
        return parent

    async def create_folder(
        self, owner_id: uuid.UUID, name: str, parent_folder_id: uuid.UUID | None
    ) -> Folder:
        parent = await self._resolve_parent(owner_id, parent_folder_id)

        if await self._folders.name_exists_in_parent(owner_id, parent_folder_id, name):
            raise DuplicateFolderException()

        path = build_child_path(parent.path if parent else None, name)
        level = (parent.level + 1) if parent else 0

        folder = Folder(
            owner_id=owner_id,
            parent_folder_id=parent_folder_id,
            name=name,
            path=path,
            level=level,
            is_root=parent_folder_id is None,
            created_by=owner_id,
            updated_by=owner_id,
        )
        folder = await self._folders.add(folder)
        await self._invalidate_folder(folder)
        await self._emit_event(
            EventType.FOLDER_CREATED,
            aggregate_type="folder",
            aggregate_id=folder.id,
            user_id=owner_id,
            payload={
                "folder_id": str(folder.id),
                "name": folder.name,
                "path": folder.path,
                "level": folder.level,
                "parent_folder_id": str(parent_folder_id) if parent_folder_id else None,
            },
        )
        return folder

    async def get_folder(self, folder_id: uuid.UUID, owner_id: uuid.UUID) -> Folder:
        return await self._get_owned_active(folder_id, owner_id)

    async def get_folder_cached(self, folder_id: uuid.UUID, owner_id: uuid.UUID) -> FolderRead:
        """Cache-aside `get_folder`, returning the API schema. See module docstring on authorization."""
        if not self._caching:
            return FolderRead.model_validate(await self._get_owned_active(folder_id, owner_id))

        key = self._cache.keys.folder(folder_id)

        async def _load() -> dict:
            # Loaded WITHOUT the owner filter on purpose: the cached entry
            # is a per-resource representation shared by any authorized
            # caller, and `_authorize_cached` applies the ownership check
            # uniformly to both the fresh and cached paths. Loading it
            # owner-filtered would make the cached value silently
            # caller-specific under a resource-scoped key — exactly the
            # bug this design is avoiding.
            folder = await self._folders.get_any_by_id(folder_id, owner_id)
            if folder is None:
                raise FolderNotFoundException()
            return FolderRead.model_validate(folder).model_dump(mode="json")

        payload = await self._cache.get_or_set(
            key, _load, self._cache.ttl_for(CacheEntity.FOLDER), entity=CacheEntity.FOLDER
        )
        return self._authorize_cached(payload, owner_id)

    async def list_children(
        self, owner_id: uuid.UUID, parent_folder_id: uuid.UUID | None, params: FolderListParams
    ) -> list[Folder]:
        if parent_folder_id is not None:
            # Validates the parent exists & is owned before listing its contents.
            await self._get_owned_active(parent_folder_id, owner_id)
        return await self._folders.list_children(owner_id, parent_folder_id, params)

    async def list_children_cached(
        self, owner_id: uuid.UUID, parent_folder_id: uuid.UUID | None, params: FolderListParams
    ) -> list[FolderRead]:
        """Cache-aside children listing, keyed per folder AND per listing parameter set."""
        if not self._caching:
            folders = await self.list_children(owner_id, parent_folder_id, params)
            return [FolderRead.model_validate(f) for f in folders]

        if parent_folder_id is not None:
            # Authorize the parent first (itself cache-aside), so a
            # non-owner never reaches the listing key at all.
            await self.get_folder_cached(parent_folder_id, owner_id)

        key = self._cache.keys.folder_children(
            parent_folder_id, owner_id, self._listing_params(params)
        )

        async def _load() -> list[dict]:
            folders = await self._folders.list_children(owner_id, parent_folder_id, params)
            return [FolderRead.model_validate(f).model_dump(mode="json") for f in folders]

        payload = await self._cache.get_or_set(
            key,
            _load,
            self._cache.ttl_for(CacheEntity.FOLDER_CHILDREN),
            entity=CacheEntity.FOLDER_CHILDREN,
        )
        return [FolderRead.model_validate(item) for item in payload]

    async def rename_folder(self, folder_id: uuid.UUID, owner_id: uuid.UUID, new_name: str, actor_id: uuid.UUID) -> Folder:
        folder = await self._get_owned_active(folder_id, owner_id)

        if folder.name == new_name:
            return folder

        if await self._folders.name_exists_in_parent(
            owner_id, folder.parent_folder_id, new_name, exclude_id=folder.id
        ):
            raise DuplicateFolderException()

        old_path = folder.path
        parent_path = old_path.rsplit("/", 1)[0] if "/" in old_path.rstrip("/") else ""
        new_path = build_child_path(parent_path or None, new_name)

        # Captured BEFORE the path mutation below, while `folder.path` is
        # still `old_path` — `list_descendants` matches on path prefix, so
        # it must run against the pre-rename tree. IDs are stable across
        # the rewrite, so this set is still exactly right after it.
        descendants = (
            await self._folders.list_descendants(folder, owner_id) if old_path != new_path else []
        )

        folder.name = new_name
        folder.path = new_path
        folder.updated_by = actor_id

        if old_path != new_path:
            await self._folders.cascade_rename(folder, old_path, new_path)

        await self._invalidate_folder(folder)
        if self._invalidator is not None and descendants:
            await self._invalidator.descendant_breadcrumbs_changed([d.id for d in descendants])
        return folder

    async def move_folder(
        self, folder_id: uuid.UUID, owner_id: uuid.UUID, new_parent_folder_id: uuid.UUID | None, actor_id: uuid.UUID
    ) -> Folder:
        folder = await self._get_owned_active(folder_id, owner_id)

        if folder.parent_folder_id == new_parent_folder_id:
            return folder  # no-op move

        if new_parent_folder_id == folder.id:
            raise CircularReferenceException(detail="Cannot move a folder into itself.")

        new_parent = await self._resolve_parent(owner_id, new_parent_folder_id)

        if new_parent is not None and is_same_or_descendant_path(new_parent.path, folder.path):
            raise CircularReferenceException()

        if await self._folders.name_exists_in_parent(owner_id, new_parent_folder_id, folder.name):
            raise DuplicateFolderException(
                detail="A folder with this name already exists in the destination."
            )

        old_parent_folder_id = folder.parent_folder_id
        old_path = folder.path
        old_level = folder.level
        new_path = build_child_path(new_parent.path if new_parent else None, folder.name)
        new_level = (new_parent.level + 1) if new_parent else 0

        # Same reasoning as `rename_folder`: gather descendants against the
        # still-old `folder.path` before it's mutated below.
        descendants = (
            await self._folders.list_descendants(folder, owner_id) if old_path != new_path else []
        )

        folder.parent_folder_id = new_parent_folder_id
        folder.is_root = new_parent_folder_id is None
        folder.path = new_path
        folder.level = new_level
        folder.updated_by = actor_id

        if old_path != new_path:
            await self._folders.cascade_rename(folder, old_path, new_path)
        level_delta = new_level - old_level
        if level_delta != 0:
            await self._folders.cascade_level_shift(folder, level_delta)

        if self._invalidator is not None:
            await self._invalidator.folder_moved(
                folder.id, folder.owner_id, old_parent_folder_id, new_parent_folder_id
            )
            if descendants:
                await self._invalidator.descendant_breadcrumbs_changed([d.id for d in descendants])
        return folder

    async def delete_folder(self, folder_id: uuid.UUID, owner_id: uuid.UUID, actor_id: uuid.UUID) -> None:
        """Soft delete: this folder AND every descendant move into the trash together."""
        folder = await self._get_owned_active(folder_id, owner_id)

        now = datetime.now(timezone.utc)
        descendants = await self._folders.list_descendants(folder, owner_id)

        for node in [folder, *descendants]:
            node.is_deleted = True
            node.deleted_at = now
            node.deleted_by = actor_id
            node.updated_by = actor_id

        # Descendants are invalidated individually (not just the root of
        # the subtree): each has its own `nimbusfs:folder:{id}` entry whose
        # `is_deleted` flag just changed, and a stale one would let a
        # trashed folder keep resolving as active for a full TTL.
        await self._invalidate_folder(folder)
        for node in descendants:
            await self._invalidate_folder(node)

        # ONE event for the top-level folder the user actually deleted —
        # not one per descendant. A recursive soft-delete of a 5,000-folder
        # subtree is a single user intent; fanning it out per node would
        # publish 5,000 messages describing one action, blow the outbox
        # batch size, and force every consumer to re-derive that they were
        # all part of the same operation. The descendant IDs ride along in
        # the payload, so a consumer that genuinely needs them has them.
        await self._emit_event(
            EventType.FOLDER_DELETED,
            aggregate_type="folder",
            aggregate_id=folder.id,
            user_id=owner_id,
            payload={
                "folder_id": str(folder.id),
                "name": folder.name,
                "path": folder.path,
                "soft_delete": True,
                "descendant_folder_ids": [str(node.id) for node in descendants],
                "descendant_count": len(descendants),
            },
        )

    async def restore_folder(self, folder_id: uuid.UUID, owner_id: uuid.UUID, actor_id: uuid.UUID) -> Folder:
        folder = await self._folders.get_any_by_id(folder_id, owner_id)
        if folder is None or not folder.is_deleted:
            raise FolderNotFoundException(detail="Folder not found in trash.")

        # Restoring a folder also restores its descendants that were
        # deleted as part of the same operation (i.e. everything currently
        # soft-deleted under its path), but does NOT resurrect items that
        # were independently deleted before this folder was.
        descendants = await self._folders.list_descendants(folder, owner_id)

        folder.is_deleted = False
        folder.deleted_at = None
        folder.deleted_by = None
        folder.updated_by = actor_id

        restored: list[Folder] = []
        for node in descendants:
            if node.is_deleted:
                node.is_deleted = False
                node.deleted_at = None
                node.deleted_by = None
                node.updated_by = actor_id
                restored.append(node)

        await self._invalidate_folder(folder)
        for node in restored:
            await self._invalidate_folder(node)
        return folder

    async def permanent_delete_folder(self, folder_id: uuid.UUID, owner_id: uuid.UUID) -> None:
        folder = await self._folders.get_any_by_id(folder_id, owner_id)
        if folder is None:
            raise FolderNotFoundException()
        if not folder.is_deleted:
            raise ValidationException(detail="Folder must be moved to trash before it can be permanently deleted.")

        # Descendants are captured BEFORE the delete: afterwards the rows
        # are gone (FK cascade) and there is nothing left to enumerate, so
        # their cache entries would be unreachable orphans until TTL.
        descendants = await self._folders.list_descendants(folder, owner_id)

        # Physical delete cascades to descendant folders via the FK's
        # ON DELETE CASCADE, and to contained files via FileMetadata's
        # own FK to folders (also ON DELETE CASCADE).
        await self._folders.delete(folder)

        await self._invalidate_folder(folder)
        for node in descendants:
            await self._invalidate_folder(node)

    async def get_tree(self, owner_id: uuid.UUID, root_folder_id: uuid.UUID | None) -> list[FolderTreeNode]:
        """
        Builds the folder tree as nested `FolderTreeNode`s.

        `root_folder_id=None` returns a forest: one tree per top-level
        folder the owner has. Otherwise returns a single tree rooted at
        the given folder.
        """
        all_folders = await self._list_all_active(owner_id)

        if root_folder_id is None:
            roots = [f for f in all_folders if f.parent_folder_id is None]
        else:
            root = await self._get_owned_active(root_folder_id, owner_id)
            roots = [root]

        by_parent: dict[uuid.UUID | None, list[Folder]] = {}
        for f in all_folders:
            by_parent.setdefault(f.parent_folder_id, []).append(f)

        def build_node(folder: Folder) -> FolderTreeNode:
            children = sorted(by_parent.get(folder.id, []), key=lambda f: f.name.lower())
            return FolderTreeNode(
                id=folder.id,
                name=folder.name,
                path=folder.path,
                level=folder.level,
                children=[build_node(child) for child in children],
            )

        return [build_node(r) for r in sorted(roots, key=lambda f: f.name.lower())]

    async def _list_all_active(self, owner_id: uuid.UUID) -> list[Folder]:
        # Deliberately simple: fetch every active folder for the owner in
        # one query, then build the tree in memory. Folder trees per user
        # are not expected to be large enough (thousands, not millions) to
        # need recursive-CTE pagination for this operation.
        return await self._folders.list_all_active(owner_id)

    async def get_breadcrumb(self, folder_id: uuid.UUID, owner_id: uuid.UUID) -> list[BreadcrumbItem]:
        folder = await self._get_owned_active(folder_id, owner_id)

        chain: list[Folder] = [folder]
        current = folder
        while current.parent_folder_id is not None:
            parent = await self._folders.get_active_by_id(current.parent_folder_id, owner_id)
            if parent is None:
                break
            chain.append(parent)
            current = parent

        chain.reverse()
        return [BreadcrumbItem(id=f.id, name=f.name, path=f.path) for f in chain]

    async def get_breadcrumb_cached(self, folder_id: uuid.UUID, owner_id: uuid.UUID) -> list[BreadcrumbItem]:
        """
        Cache-aside breadcrumb trail.

        Worth caching specifically because the uncached version costs one
        query PER ANCESTOR (see `get_breadcrumb`'s parent-walk loop) — a
        folder ten levels deep is eleven round trips to Postgres to render
        one navigation bar, on a request that is otherwise trivial. This
        is the single highest query-count-per-byte read in the API.
        """
        if not self._caching:
            return await self.get_breadcrumb(folder_id, owner_id)

        # Authorize first — the cached trail is keyed by folder only.
        await self.get_folder_cached(folder_id, owner_id)

        key = self._cache.keys.folder_breadcrumbs(folder_id)

        async def _load() -> list[dict]:
            items = await self.get_breadcrumb(folder_id, owner_id)
            return [item.model_dump(mode="json") for item in items]

        payload = await self._cache.get_or_set(
            key,
            _load,
            self._cache.ttl_for(CacheEntity.FOLDER_BREADCRUMBS),
            entity=CacheEntity.FOLDER_BREADCRUMBS,
        )
        return [BreadcrumbItem.model_validate(item) for item in payload]

    async def list_trash(self, owner_id: uuid.UUID) -> list[Folder]:
        return await self._folders.list_trash(owner_id)