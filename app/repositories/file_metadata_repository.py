"""
File metadata repository.

Design decision: `search` joins `Folder` (outer join, since `folder_id`
is nullable for top-level files) so a query term can match either the
filename or the containing folder's name in one query, matching the
product behavior of "search finds files by name or by the folder
they're filed under."
"""

import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file_metadata import FileMetadata
from app.models.folder import Folder
from app.repositories.base import BaseRepository
from app.schemas.search import FileSearchParams
from app.schemas.sorting import FileSortField, SortOrder


class FileMetadataRepository(BaseRepository[FileMetadata]):
    model = FileMetadata

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_active_by_id(self, file_id: uuid.UUID, owner_id: uuid.UUID) -> FileMetadata | None:
        result = await self._session.execute(
            select(FileMetadata).where(
                FileMetadata.id == file_id,
                FileMetadata.owner_id == owner_id,
                FileMetadata.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def get_any_by_id(self, file_id: uuid.UUID, owner_id: uuid.UUID) -> FileMetadata | None:
        """Fetches regardless of soft-delete state (used by restore/permanent-delete)."""
        result = await self._session.execute(
            select(FileMetadata).where(FileMetadata.id == file_id, FileMetadata.owner_id == owner_id)
        )
        return result.scalar_one_or_none()

    async def name_exists_in_folder(
        self,
        owner_id: uuid.UUID,
        folder_id: uuid.UUID | None,
        original_filename: str,
        exclude_id: uuid.UUID | None = None,
    ) -> bool:
        conditions = [
            FileMetadata.owner_id == owner_id,
            FileMetadata.original_filename == original_filename,
            FileMetadata.is_deleted.is_(False),
            FileMetadata.folder_id == folder_id
            if folder_id is not None
            else FileMetadata.folder_id.is_(None),
        ]
        if exclude_id is not None:
            conditions.append(FileMetadata.id != exclude_id)

        result = await self._session.execute(select(FileMetadata.id).where(and_(*conditions)).limit(1))
        return result.scalar_one_or_none() is not None

    async def get_by_checksum(self, owner_id: uuid.UUID, checksum: str) -> FileMetadata | None:
        """
        Finds an existing, non-deleted, successfully-uploaded file with
        identical content for this owner — used by FileUploadService to
        dedupe storage bytes (see its docstring for the full rationale).
        """
        result = await self._session.execute(
            select(FileMetadata)
            .where(
                FileMetadata.owner_id == owner_id,
                FileMetadata.checksum == checksum,
                FileMetadata.is_deleted.is_(False),
                FileMetadata.object_name.is_not(None),
            )
            .order_by(FileMetadata.created_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def object_name_in_use(self, object_name: str, exclude_id: uuid.UUID | None = None) -> bool:
        """True if any non-deleted row (other than `exclude_id`) still points at this GCS object name."""
        conditions = [FileMetadata.object_name == object_name, FileMetadata.is_deleted.is_(False)]
        if exclude_id is not None:
            conditions.append(FileMetadata.id != exclude_id)
        result = await self._session.execute(select(FileMetadata.id).where(and_(*conditions)).limit(1))
        return result.scalar_one_or_none() is not None

    async def list_trash(self, owner_id: uuid.UUID) -> list[FileMetadata]:
        result = await self._session.execute(
            select(FileMetadata)
            .where(FileMetadata.owner_id == owner_id, FileMetadata.is_deleted.is_(True))
            .order_by(FileMetadata.deleted_at.desc())
        )
        return list(result.scalars().all())

    async def search(
        self, owner_id: uuid.UUID, params: FileSearchParams, offset: int, limit: int
    ) -> tuple[list[FileMetadata], int]:
        conditions = [FileMetadata.owner_id == owner_id]

        if params.q:
            like_term = f"%{params.q}%"
            conditions.append(
                or_(FileMetadata.original_filename.ilike(like_term), Folder.name.ilike(like_term))
            )
        if params.folder_id is not None:
            conditions.append(FileMetadata.folder_id == params.folder_id)
        if params.extension:
            conditions.append(FileMetadata.extension == params.extension)
        if params.mime_type:
            conditions.append(FileMetadata.mime_type == params.mime_type)
        if params.version is not None:
            conditions.append(FileMetadata.version == params.version)
        if params.is_deleted is not None:
            conditions.append(FileMetadata.is_deleted.is_(params.is_deleted))
        else:
            conditions.append(FileMetadata.is_deleted.is_(False))
        if params.created_after is not None:
            conditions.append(FileMetadata.created_at >= params.created_after)
        if params.created_before is not None:
            conditions.append(FileMetadata.created_at <= params.created_before)
        if params.updated_after is not None:
            conditions.append(FileMetadata.updated_at >= params.updated_after)
        if params.updated_before is not None:
            conditions.append(FileMetadata.updated_at <= params.updated_before)

        sort_column = {
            FileSortField.NAME: FileMetadata.original_filename,
            FileSortField.CREATED_AT: FileMetadata.created_at,
            FileSortField.UPDATED_AT: FileMetadata.updated_at,
            FileSortField.SIZE: FileMetadata.size,
            FileSortField.TYPE: FileMetadata.extension,
        }[params.sort_by]
        order = sort_column.asc() if params.sort_order == SortOrder.ASC else sort_column.desc()

        base_query = select(FileMetadata).outerjoin(Folder, FileMetadata.folder_id == Folder.id).where(
            and_(*conditions)
        )

        count_result = await self._session.execute(
            select(func.count()).select_from(base_query.with_only_columns(FileMetadata.id).subquery())
        )
        total = count_result.scalar_one()

        result = await self._session.execute(
            base_query.order_by(order).offset(offset).limit(limit)
        )
        return list(result.scalars().all()), total
