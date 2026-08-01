"""FileVersion repository — append-only version history access."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file_version import FileVersion
from app.repositories.base import BaseRepository


class FileVersionRepository(BaseRepository[FileVersion]):
    model = FileVersion

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_for_file(self, file_id: uuid.UUID) -> list[FileVersion]:
        result = await self._session.execute(
            select(FileVersion).where(FileVersion.file_id == file_id).order_by(FileVersion.version.desc())
        )
        return list(result.scalars().all())

    async def create(self, file_id: uuid.UUID, version: int, checksum: str | None, size: int) -> FileVersion:
        record = FileVersion(file_id=file_id, version=version, checksum=checksum, size=size)
        return await self.add(record)