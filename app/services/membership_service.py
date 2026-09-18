"""
`MembershipService` — organization membership management (Phase 13
§8-9, §24-25).

Every method here is called from an org-scoped route already gated by
`require_org_role(...)` (`app/dependencies/organization.py`) — this
service does NOT re-check the caller's own role; it enforces the
narrower, resource-specific invariant that IS its job: an organization
must never end up with zero active OWNERs (see `remove_member`/
`change_role`), which is a business rule, not an authorization
decision, and belongs here rather than in the dependency layer.
"""

from __future__ import annotations

import uuid

from app.core.enums import AuditEventType, AuditResult, OrganizationRole
from app.exceptions.custom_exceptions import ConflictException, LastOwnerException, MembershipNotFoundException
from app.logging.logger import get_logger
from app.models.organization_membership import OrganizationMembership
from app.repositories.membership_repository import MembershipRepository
from app.repositories.user_repository import UserRepository
from app.services.audit_service import AuditService

logger = get_logger(__name__)


class MembershipService:
    def __init__(
        self,
        membership_repository: MembershipRepository,
        user_repository: UserRepository,
        *,
        audit: AuditService | None = None,
    ):
        self._memberships = membership_repository
        self._users = user_repository
        self._audit = audit

    async def add_member(
        self, actor_user_id: uuid.UUID, organization_id: uuid.UUID, email: str, role: OrganizationRole
    ) -> OrganizationMembership:
        user = await self._users.get_by_email(email)
        if user is None:
            # Deliberately the SAME `NotFoundException`-family shape as
            # every other "resource not found" in this codebase, not an
            # "invite by email" flow (no email-verification/invitation
            # token system exists yet — out of scope, see
            # `docs/administration.md`'s remaining-risks list). The user
            # must already have a NimbusFS account.
            raise MembershipNotFoundException(detail="No user exists with that email address.")

        existing = await self._memberships.get_active(user.id, organization_id)
        if existing is not None:
            raise ConflictException(detail="This user is already a member of the organization.")

        membership = OrganizationMembership(organization_id=organization_id, user_id=user.id, role=role)
        membership = await self._memberships.add(membership)

        if self._audit is not None:
            await self._audit.record(
                AuditEventType.MEMBER_ADDED,
                result=AuditResult.SUCCESS,
                actor_user_id=actor_user_id,
                organization_id=organization_id,
                resource_type="user",
                resource_id=user.id,
                detail={"role": role.value},
            )
        logger.info(
            "organization_member_added", organization_id=str(organization_id), user_id=str(user.id), role=role.value
        )
        return membership

    async def change_role(
        self, actor_user_id: uuid.UUID, organization_id: uuid.UUID, member_user_id: uuid.UUID, new_role: OrganizationRole
    ) -> OrganizationMembership:
        membership = await self._memberships.get_active(member_user_id, organization_id)
        if membership is None:
            raise MembershipNotFoundException()

        if (
            membership.role == OrganizationRole.OWNER
            and new_role != OrganizationRole.OWNER
            and await self._memberships.count_active_owners(organization_id) <= 1
        ):
            raise LastOwnerException(detail="Cannot demote the organization's only remaining owner.")

        previous_role = membership.role
        membership.role = new_role
        await self._memberships.flush()

        if self._audit is not None:
            await self._audit.record(
                AuditEventType.MEMBER_ROLE_CHANGED,
                result=AuditResult.SUCCESS,
                actor_user_id=actor_user_id,
                organization_id=organization_id,
                resource_type="user",
                resource_id=member_user_id,
                detail={"previous_role": previous_role.value, "new_role": new_role.value},
            )
        return membership

    async def remove_member(self, actor_user_id: uuid.UUID, organization_id: uuid.UUID, member_user_id: uuid.UUID) -> None:
        membership = await self._memberships.get_active(member_user_id, organization_id)
        if membership is None:
            raise MembershipNotFoundException()

        if (
            membership.role == OrganizationRole.OWNER
            and await self._memberships.count_active_owners(organization_id) <= 1
        ):
            raise LastOwnerException(detail="Cannot remove the organization's only remaining owner.")

        await self._memberships.remove(membership)

        if self._audit is not None:
            await self._audit.record(
                AuditEventType.MEMBER_REMOVED,
                result=AuditResult.SUCCESS,
                actor_user_id=actor_user_id,
                organization_id=organization_id,
                resource_type="user",
                resource_id=member_user_id,
            )
        logger.info("organization_member_removed", organization_id=str(organization_id), user_id=str(member_user_id))

    async def list_members(
        self, organization_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[OrganizationMembership], int]:
        return await self._memberships.list_for_organization(organization_id, limit=limit, offset=offset)
