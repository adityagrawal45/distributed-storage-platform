"""
Folder repository.

Design decision: the materialized-path cascade updates (`cascade_rename`,
`cascade_move`) live here, not in the service, because they're pure
bulk-SQL operations over the table — no business rules, just "update
every descendant's path/level in one statement". Keeping them in the
repository means `FolderService` only orchestrates *what* changed; *how*
that's persisted efficiently is a repository concern.
"""

import uuid

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.folder import Folder
from app.repositories.base import BaseRepository
from app.schemas.search import FolderListParams
from app.schemas.sorting import FolderSortField, SortOrder


class FolderRepository(BaseRepository[Folder]):
    model = Folder

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_active_by_id(self, folder_id: uuid.UUID, owner_id: uuid.UUID) -> Folder | None:
        result = await self._session.execute(
            select(Folder).where(
                Folder.id == folder_id, Folder.owner_id == owner_id, Folder.is_deleted.is_(False)
            )
        )
        return result.scalar_one_or_none()

    async def get_any_by_id(self, folder_id: uuid.UUID, owner_id: uuid.UUID) -> Folder | None:
        """Fetches regardless of soft-delete state (used by restore/permanent-delete)."""
        result = await self._session.execute(
            select(Folder).where(Folder.id == folder_id, Folder.owner_id == owner_id)
        )
        return result.scalar_one_or_none()

    async def name_exists_in_parent(
        self,
        owner_id: uuid.UUID,
        parent_folder_id: uuid.UUID | None,
        name: str,
        exclude_id: uuid.UUID | None = None,
    ) -> bool:
        conditions = [
            Folder.owner_id == owner_id,
            Folder.name == name,
            Folder.is_deleted.is_(False),
            Folder.parent_folder_id == parent_folder_id
            if parent_folder_id is not None
            else Folder.parent_folder_id.is_(None),
        ]
        if exclude_id is not None:
            conditions.append(Folder.id != exclude_id)

        result = await self._session.execute(select(Folder.id).where(and_(*conditions)).limit(1))
        return result.scalar_one_or_none() is not None

    async def list_children(
        self, owner_id: uuid.UUID, parent_folder_id: uuid.UUID | None, params: FolderListParams
    ) -> list[Folder]:
        conditions = [
            Folder.owner_id == owner_id,
            Folder.parent_folder_id == parent_folder_id
            if parent_folder_id is not None
            else Folder.parent_folder_id.is_(None),
        ]
        if params.is_deleted is not None:
            conditions.append(Folder.is_deleted.is_(params.is_deleted))
        else:
            conditions.append(Folder.is_deleted.is_(False))

        sort_column = {
            FolderSortField.NAME: Folder.name,
            FolderSortField.CREATED_AT: Folder.created_at,
            FolderSortField.UPDATED_AT: Folder.updated_at,
        }[params.sort_by]
        order = sort_column.asc() if params.sort_order == SortOrder.ASC else sort_column.desc()

        result = await self._session.execute(select(Folder).where(and_(*conditions)).order_by(order))
        return list(result.scalars().all())

    async def list_descendants(self, folder: Folder, owner_id: uuid.UUID) -> list[Folder]:
        """All folders whose path is nested under `folder.path` (any depth)."""
        prefix = folder.path.rstrip("/") + "/"
        result = await self._session.execute(
            select(Folder).where(Folder.owner_id == owner_id, Folder.path.like(f"{prefix}%"))
        )
        return list(result.scalars().all())

    async def list_all_active(self, owner_id: uuid.UUID) -> list[Folder]:
        """All non-deleted folders for an owner, in one query (used to build the full tree in memory)."""
        result = await self._session.execute(
            select(Folder).where(Folder.owner_id == owner_id, Folder.is_deleted.is_(False))
        )
        return list(result.scalars().all())

    async def list_trash(self, owner_id: uuid.UUID) -> list[Folder]:
        result = await self._session.execute(
            select(Folder)
            .where(Folder.owner_id == owner_id, Folder.is_deleted.is_(True))
            .order_by(Folder.deleted_at.desc())
        )
        return list(result.scalars().all())

    async def cascade_rename(self, folder: Folder, old_path: str, new_path: str) -> None:
        """
        Rewrites the path prefix for every descendant after a rename/move.

        Deliberately computed in Python rather than via SQL `concat`/`substr`,
        since those functions differ enough across dialects (notably SQLite,
        used in the test suite, vs. PostgreSQL in production) that a raw-SQL
        version would need dialect-specific branches. Folder trees are not
        high-cardinality enough for this to be a meaningful cost.
        """
        old_prefix = old_path.rstrip("/") + "/"
        new_prefix = new_path.rstrip("/") + "/"

        result = await self._session.execute(
            select(Folder).where(Folder.owner_id == folder.owner_id, Folder.path.like(f"{old_prefix}%"))
        )
        for descendant in result.scalars().all():
            descendant.path = new_prefix + descendant.path[len(old_prefix):]
        await self._session.flush()

    async def cascade_level_shift(self, folder: Folder, level_delta: int) -> None:
        """Applies a level offset to every descendant after a move changes depth."""
        if level_delta == 0:
            return
        prefix = folder.path.rstrip("/") + "/"
        await self._session.execute(
            update(Folder)
            .where(Folder.owner_id == folder.owner_id, Folder.path.like(f"{prefix}%"))
            .values(level=Folder.level + level_delta)
        )
        await self._session.flush()