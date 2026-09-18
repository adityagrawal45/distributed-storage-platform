"""Organization repository (Phase 13)."""

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import OrganizationStatus
from app.models.organization import Organization
from app.repositories.base import BaseRepository


class OrganizationRepository(BaseRepository[Organization]):
    model = Organization

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_slug(self, slug: str) -> Organization | None:
        result = await self._session.execute(select(Organization).where(Organization.slug == slug))
        return result.scalar_one_or_none()

    async def slug_exists(self, slug: str) -> bool:
        result = await self._session.execute(select(Organization.id).where(Organization.slug == slug).limit(1))
        return result.scalar_one_or_none() is not None

    async def set_status(self, organization_id: uuid.UUID, status: OrganizationStatus) -> None:
        await self._session.execute(
            update(Organization).where(Organization.id == organization_id).values(status=status)
        )
        await self._session.flush()

    async def try_reserve_storage(self, organization_id: uuid.UUID, delta_bytes: int, delta_files: int) -> bool:
        """
        Atomically applies a storage/file-count delta, **only if the
        result would not exceed `storage_limit_bytes`** — a single
        guarded `UPDATE`, not a separate SELECT-then-UPDATE (Phase 13
        §21's explicit warning: "check quota -> upload -> update quota"
        is a race between two concurrent uploads that can BOTH pass the
        check before either commits). Postgres evaluates the WHERE
        clause against the CURRENT row version under the transaction's
        isolation level, and a second concurrent `UPDATE` targeting the
        same row blocks behind the first's row lock until it commits —
        there is no window where two callers both see "room available"
        for the same bytes. `NULL` limit means unlimited (see
        `Organization.storage_limit_bytes`'s docstring), expressed as
        the WHERE clause simply not applying that arm of the OR.

        Returns True if the reservation succeeded (row updated), False
        if it would have exceeded the limit (row NOT updated — caller
        must not proceed with the upload). `delta_bytes`/`delta_files`
        may be negative (a delete/cancel releasing quota) — the limit
        check only applies to a POSITIVE net change; see `QuotaService`
        for why a release never needs to be "guarded" the same way.
        """
        result = await self._session.execute(
            update(Organization)
            .where(
                Organization.id == organization_id,
                (Organization.storage_limit_bytes.is_(None))
                | (Organization.storage_used_bytes + delta_bytes <= Organization.storage_limit_bytes),
            )
            .values(
                storage_used_bytes=Organization.storage_used_bytes + delta_bytes,
                file_count=Organization.file_count + delta_files,
            )
        )
        await self._session.flush()
        return result.rowcount == 1

    async def release_storage(self, organization_id: uuid.UUID, delta_bytes: int, delta_files: int) -> None:
        """Unconditional adjustment (deletion/cancellation releasing quota) — never guarded, never fails."""
        await self._session.execute(
            update(Organization)
            .where(Organization.id == organization_id)
            .values(
                storage_used_bytes=Organization.storage_used_bytes + delta_bytes,
                file_count=Organization.file_count + delta_files,
            )
        )
        await self._session.flush()
