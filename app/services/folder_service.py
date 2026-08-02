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
"""

import uuid
from datetime import datetime, timezone

from app.exceptions.custom_exceptions import (
    CircularReferenceException,
    DuplicateFolderException,
    FolderNotFoundException,
    ValidationException,
)
from app.models.folder import Folder
from app.repositories.folder_repository import FolderRepository
from app.schemas.folder import BreadcrumbItem, FolderTreeNode
from app.schemas.search import FolderListParams
from app.utils.path_utils import build_child_path, is_same_or_descendant_path


class FolderService:
    def __init__(self, folder_repository: FolderRepository):
        self._folders = folder_repository

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
        return await self._folders.add(folder)

    async def get_folder(self, folder_id: uuid.UUID, owner_id: uuid.UUID) -> Folder:
        return await self._get_owned_active(folder_id, owner_id)

    async def list_children(
        self, owner_id: uuid.UUID, parent_folder_id: uuid.UUID | None, params: FolderListParams
    ) -> list[Folder]:
        if parent_folder_id is not None:
            # Validates the parent exists & is owned before listing its contents.
            await self._get_owned_active(parent_folder_id, owner_id)
        return await self._folders.list_children(owner_id, parent_folder_id, params)

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

        folder.name = new_name
        folder.path = new_path
        folder.updated_by = actor_id

        if old_path != new_path:
            await self._folders.cascade_rename(folder, old_path, new_path)

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

        old_path = folder.path
        old_level = folder.level
        new_path = build_child_path(new_parent.path if new_parent else None, folder.name)
        new_level = (new_parent.level + 1) if new_parent else 0

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

        for node in descendants:
            if node.is_deleted:
                node.is_deleted = False
                node.deleted_at = None
                node.deleted_by = None
                node.updated_by = actor_id

        return folder

    async def permanent_delete_folder(self, folder_id: uuid.UUID, owner_id: uuid.UUID) -> None:
        folder = await self._folders.get_any_by_id(folder_id, owner_id)
        if folder is None:
            raise FolderNotFoundException()
        if not folder.is_deleted:
            raise ValidationException(detail="Folder must be moved to trash before it can be permanently deleted.")

        # Physical delete cascades to descendant folders via the FK's
        # ON DELETE CASCADE, and to contained files via FileMetadata's
        # own FK to folders (also ON DELETE CASCADE).
        await self._folders.delete(folder)

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

    async def list_trash(self, owner_id: uuid.UUID) -> list[Folder]:
        return await self._folders.list_trash(owner_id)