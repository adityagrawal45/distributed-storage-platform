"""`GroupService` — organization-scoped groups (Phase 13 §10)."""

from __future__ import annotations

import uuid

from app.exceptions.custom_exceptions import ConflictException, GroupNotFoundException, MembershipNotFoundException
from app.models.group import Group, GroupMembership
from app.repositories.group_repository import GroupRepository
from app.repositories.membership_repository import MembershipRepository


class GroupService:
    def __init__(self, group_repository: GroupRepository, membership_repository: MembershipRepository):
        self._groups = group_repository
        self._memberships = membership_repository

    async def create(self, organization_id: uuid.UUID, name: str) -> Group:
        if await self._groups.name_exists(organization_id, name):
            raise ConflictException(detail="A group with this name already exists in this organization.")
        return await self._groups.add(Group(organization_id=organization_id, name=name))

    async def get(self, group_id: uuid.UUID, organization_id: uuid.UUID) -> Group:
        group = await self._groups.get_active_by_id(group_id, organization_id)
        if group is None:
            raise GroupNotFoundException()
        return group

    async def list_for_organization(self, organization_id: uuid.UUID) -> list[Group]:
        return await self._groups.list_for_organization(organization_id)

    async def add_member(self, group_id: uuid.UUID, organization_id: uuid.UUID, member_user_id: uuid.UUID) -> GroupMembership:
        await self.get(group_id, organization_id)  # 404s if the group isn't in this org
        # The user must be an active member of the SAME organization —
        # a group cannot grant a foothold to someone outside the tenant
        # (Phase 13's core invariant, applied to group membership too).
        if await self._memberships.get_active(member_user_id, organization_id) is None:
            raise MembershipNotFoundException(detail="That user is not a member of this organization.")
        if await self._groups.is_member(group_id, member_user_id):
            raise ConflictException(detail="That user is already a member of this group.")
        return await self._groups.add_member(group_id, member_user_id)

    async def remove_member(self, group_id: uuid.UUID, organization_id: uuid.UUID, member_user_id: uuid.UUID) -> None:
        await self.get(group_id, organization_id)
        await self._groups.remove_member(group_id, member_user_id)

    async def list_members(self, group_id: uuid.UUID, organization_id: uuid.UUID) -> list[GroupMembership]:
        await self.get(group_id, organization_id)
        return await self._groups.list_members(group_id)
