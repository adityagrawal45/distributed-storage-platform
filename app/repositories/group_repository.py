"""Group / GroupMembership repositories (Phase 13)."""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import Group, GroupMembership
from app.repositories.base import BaseRepository


class GroupRepository(BaseRepository[Group]):
    model = Group

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_active_by_id(self, group_id: uuid.UUID, organization_id: uuid.UUID) -> Group | None:
        """Named `get_active_by_id` to match the existing `Folder`/`FileMetadata` repository convention
        even though `Group` has no soft-delete — "active" here means "in this caller's organization"."""
        result = await self._session.execute(
            select(Group).where(Group.id == group_id, Group.organization_id == organization_id)
        )
        return result.scalar_one_or_none()

    async def name_exists(self, organization_id: uuid.UUID, name: str) -> bool:
        result = await self._session.execute(
            select(Group.id).where(Group.organization_id == organization_id, Group.name == name).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def list_for_organization(self, organization_id: uuid.UUID) -> list[Group]:
        result = await self._session.execute(
            select(Group).where(Group.organization_id == organization_id).order_by(Group.name.asc())
        )
        return list(result.scalars().all())

    async def list_group_ids_for_user(self, user_id: uuid.UUID, organization_id: uuid.UUID) -> list[uuid.UUID]:
        """
        Every group (within ONE organization) a user belongs to — the
        set `PermissionResolver` unions with the user's own `user_id`
        when checking `ResourcePermission` grants. Scoped by
        `organization_id` via a join so a user's group membership in
        Org A can never be consulted while evaluating access in Org B
        (a user could theoretically be in same-named groups in two
        different orgs — they are different `Group` rows with different
        `id`s, so this is correct by construction, not by convention).
        """
        result = await self._session.execute(
            select(GroupMembership.group_id)
            .join(Group, Group.id == GroupMembership.group_id)
            .where(GroupMembership.user_id == user_id, Group.organization_id == organization_id)
        )
        return list(result.scalars().all())

    async def add_member(self, group_id: uuid.UUID, user_id: uuid.UUID) -> GroupMembership:
        membership = GroupMembership(group_id=group_id, user_id=user_id)
        self._session.add(membership)
        await self.flush()
        return membership

    async def remove_member(self, group_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(GroupMembership).where(GroupMembership.group_id == group_id, GroupMembership.user_id == user_id)
        )
        await self.flush()

    async def is_member(self, group_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            select(GroupMembership.id).where(
                GroupMembership.group_id == group_id, GroupMembership.user_id == user_id
            )
        )
        return result.scalar_one_or_none() is not None

    async def list_members(self, group_id: uuid.UUID) -> list[GroupMembership]:
        result = await self._session.execute(select(GroupMembership).where(GroupMembership.group_id == group_id))
        return list(result.scalars().all())
