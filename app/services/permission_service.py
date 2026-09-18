"""
`PermissionService` — grants/revokes direct `ResourcePermission` rows
(Phase 13 §11-13). The READ side of authorization ("can this user do
X?") lives in `app/core/authorization.py::PermissionResolver`; this is
the WRITE side ("grant/revoke Y") — kept as two classes because
granting a permission and CHECKING one have almost no code in common
and different callers (routes that manage sharing vs. every route that
touches a resource at all).
"""

from __future__ import annotations

import uuid

from app.core.enums import AuditEventType, AuditResult, Permission, PrincipalType, ResourceType
from app.exceptions.custom_exceptions import GroupNotFoundException, MembershipNotFoundException
from app.models.resource_permission import ResourcePermission
from app.repositories.group_repository import GroupRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.resource_permission_repository import ResourcePermissionRepository
from app.services.audit_service import AuditService


class PermissionService:
    def __init__(
        self,
        permission_repository: ResourcePermissionRepository,
        membership_repository: MembershipRepository,
        group_repository: GroupRepository,
        *,
        audit: AuditService | None = None,
    ):
        self._permissions = permission_repository
        self._memberships = membership_repository
        self._groups = group_repository
        self._audit = audit

    async def _validate_principal(
        self, organization_id: uuid.UUID, principal_type: PrincipalType, principal_id: uuid.UUID
    ) -> None:
        """A grant's principal must belong to the SAME organization as the resource — this is what
        stops "share my folder with a user in a different tenant" from ever being expressible at all,
        independent of anything `PermissionResolver` checks at read time."""
        if principal_type == PrincipalType.USER:
            if await self._memberships.get_active(principal_id, organization_id) is None:
                raise MembershipNotFoundException(detail="That user is not a member of this organization.")
        else:
            if await self._groups.get_active_by_id(principal_id, organization_id) is None:
                raise GroupNotFoundException(detail="That group does not belong to this organization.")

    async def grant(
        self,
        *,
        actor_user_id: uuid.UUID,
        organization_id: uuid.UUID,
        resource_type: ResourceType,
        resource_id: uuid.UUID,
        principal_type: PrincipalType,
        principal_id: uuid.UUID,
        permission: Permission,
    ) -> ResourcePermission:
        await self._validate_principal(organization_id, principal_type, principal_id)
        grant = await self._permissions.grant(
            organization_id=organization_id,
            resource_type=resource_type,
            resource_id=resource_id,
            principal_type=principal_type,
            principal_id=principal_id,
            permission=permission,
            granted_by=actor_user_id,
        )
        if self._audit is not None:
            await self._audit.record(
                AuditEventType.PERMISSION_GRANTED,
                result=AuditResult.SUCCESS,
                actor_user_id=actor_user_id,
                organization_id=organization_id,
                resource_type=resource_type.value,
                resource_id=resource_id,
                detail={
                    "principal_type": principal_type.value,
                    "principal_id": str(principal_id),
                    "permission": permission.value,
                },
            )
        return grant

    async def revoke(
        self,
        *,
        actor_user_id: uuid.UUID,
        organization_id: uuid.UUID,
        resource_type: ResourceType,
        resource_id: uuid.UUID,
        principal_type: PrincipalType,
        principal_id: uuid.UUID,
        permission: Permission | None = None,
    ) -> int:
        removed = await self._permissions.revoke(
            resource_type=resource_type,
            resource_id=resource_id,
            principal_type=principal_type,
            principal_id=principal_id,
            permission=permission,
        )
        if removed and self._audit is not None:
            await self._audit.record(
                AuditEventType.PERMISSION_REVOKED,
                result=AuditResult.SUCCESS,
                actor_user_id=actor_user_id,
                organization_id=organization_id,
                resource_type=resource_type.value,
                resource_id=resource_id,
                detail={"principal_type": principal_type.value, "principal_id": str(principal_id)},
            )
        return removed

    async def list_for_resource(self, resource_type: ResourceType, resource_id: uuid.UUID) -> list[ResourcePermission]:
        return await self._permissions.list_for_resource(resource_type, resource_id)
