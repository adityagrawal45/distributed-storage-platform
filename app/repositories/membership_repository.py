"""OrganizationMembership repository (Phase 13)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MembershipStatus, OrganizationRole
from app.models.organization_membership import OrganizationMembership
from app.repositories.base import BaseRepository


class MembershipRepository(BaseRepository[OrganizationMembership]):
    model = OrganizationMembership

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_active(self, user_id: uuid.UUID, organization_id: uuid.UUID) -> OrganizationMembership | None:
        result = await self._session.execute(
            select(OrganizationMembership).where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.status == MembershipStatus.ACTIVE,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_user(self, user_id: uuid.UUID) -> list[OrganizationMembership]:
        """Every organization a user is currently an active member of — the set `CurrentOrganization`
        context-switching validates against (Phase 13 §6)."""
        result = await self._session.execute(
            select(OrganizationMembership).where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.status == MembershipStatus.ACTIVE,
            )
        )
        return list(result.scalars().all())

    async def list_for_organization(
        self, organization_id: uuid.UUID, *, limit: int = 100, offset: int = 0
    ) -> tuple[list[OrganizationMembership], int]:
        conditions = (
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.status == MembershipStatus.ACTIVE,
        )
        count_result = await self._session.execute(
            select(func.count()).select_from(OrganizationMembership).where(*conditions)
        )
        total = count_result.scalar_one()
        rows = await self._session.execute(
            select(OrganizationMembership)
            .where(*conditions)
            .order_by(OrganizationMembership.created_at.asc())
            .offset(offset)
            .limit(limit)
        )
        return list(rows.scalars().all()), total

    async def count_active(self, organization_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.status == MembershipStatus.ACTIVE,
            )
        )
        return result.scalar_one()

    async def count_active_owners(self, organization_id: uuid.UUID) -> int:
        """Used to refuse removing/demoting the last OWNER — see `MembershipService.remove_member`."""
        result = await self._session.execute(
            select(self.model.id).where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.status == MembershipStatus.ACTIVE,
                OrganizationMembership.role == OrganizationRole.OWNER,
            )
        )
        return len(result.all())

    async def remove(self, membership: OrganizationMembership) -> None:
        membership.status = MembershipStatus.REMOVED
        membership.removed_at = datetime.now(UTC)
        await self.flush()
