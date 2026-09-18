"""Share repository (Phase 13)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ResourceType
from app.models.share import Share
from app.repositories.base import BaseRepository


class ShareRepository(BaseRepository[Share]):
    model = Share

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_token_hash(self, token_hash: str) -> Share | None:
        result = await self._session.execute(select(Share).where(Share.token_hash == token_hash))
        return result.scalar_one_or_none()

    async def get_owned(self, share_id: uuid.UUID, organization_id: uuid.UUID) -> Share | None:
        result = await self._session.execute(
            select(Share).where(Share.id == share_id, Share.organization_id == organization_id)
        )
        return result.scalar_one_or_none()

    async def list_for_organization(
        self, organization_id: uuid.UUID, *, created_by: uuid.UUID | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[list[Share], int]:
        conditions = [Share.organization_id == organization_id]
        if created_by is not None:
            conditions.append(Share.created_by == created_by)
        count_result = await self._session.execute(select(func.count()).select_from(Share).where(*conditions))
        total = count_result.scalar_one()
        rows = await self._session.execute(
            select(Share).where(*conditions).order_by(Share.created_at.desc()).offset(offset).limit(limit)
        )
        return list(rows.scalars().all()), total

    async def list_for_resource(self, resource_type: ResourceType, resource_id: uuid.UUID) -> list[Share]:
        result = await self._session.execute(
            select(Share).where(Share.resource_type == resource_type, Share.resource_id == resource_id)
        )
        return list(result.scalars().all())

    async def revoke(self, share: Share) -> None:
        share.revoked_at = datetime.now(UTC)
        await self.flush()

    async def increment_download_count(self, share_id: uuid.UUID) -> None:
        """
        A plain, unconditional `+1` — the `max_downloads` ceiling itself
        is enforced by `ShareService` re-reading the row inside the SAME
        request before deciding to serve the download, not by this
        UPDATE's WHERE clause; a share redemption doesn't need the
        atomic-guarded-UPDATE treatment `Organization`'s quota does,
        because two concurrent redemptions each still get one real
        download — there's no "overspend" to prevent here, unlike bytes
        against a hard byte ceiling.
        """
        await self._session.execute(
            update(Share).where(Share.id == share_id).values(download_count=Share.download_count + 1)
        )
        await self.flush()
