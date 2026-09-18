"""ResourcePermission repository (Phase 13)."""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Permission, PrincipalType, ResourceType
from app.models.resource_permission import ResourcePermission
from app.repositories.base import BaseRepository


class ResourcePermissionRepository(BaseRepository[ResourcePermission]):
    model = ResourcePermission

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def grant(
        self,
        *,
        organization_id: uuid.UUID,
        resource_type: ResourceType,
        resource_id: uuid.UUID,
        principal_type: PrincipalType,
        principal_id: uuid.UUID,
        permission: Permission,
        granted_by: uuid.UUID | None,
    ) -> ResourcePermission:
        """
        Idempotent by construction (Phase 13 §32) — re-granting an
        identical `(resource, principal, permission)` tuple hits the
        unique index and is absorbed via a SAVEPOINT, the same
        "the constraint is the guarantee, the pre-check is the
        optimization" pattern `ProcessedEventRepository` established in
        Phase 8, rather than a fragile `if not exists: insert`.
        """
        existing = await self._get_exact(resource_type, resource_id, principal_type, principal_id, permission)
        if existing is not None:
            return existing

        entry = ResourcePermission(
            organization_id=organization_id,
            resource_type=resource_type,
            resource_id=resource_id,
            principal_type=principal_type,
            principal_id=principal_id,
            permission=permission,
            granted_by=granted_by,
        )
        self._session.add(entry)
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError:
            # Lost the idempotency race against a concurrent identical
            # grant — the winner's row already satisfies the caller.
            existing = await self._get_exact(resource_type, resource_id, principal_type, principal_id, permission)
            assert existing is not None  # the unique index guarantees a row now exists
            return existing
        return entry

    async def _get_exact(
        self,
        resource_type: ResourceType,
        resource_id: uuid.UUID,
        principal_type: PrincipalType,
        principal_id: uuid.UUID,
        permission: Permission,
    ) -> ResourcePermission | None:
        result = await self._session.execute(
            select(ResourcePermission).where(
                ResourcePermission.resource_type == resource_type,
                ResourcePermission.resource_id == resource_id,
                ResourcePermission.principal_type == principal_type,
                ResourcePermission.principal_id == principal_id,
                ResourcePermission.permission == permission,
            )
        )
        return result.scalar_one_or_none()

    async def revoke(
        self,
        *,
        resource_type: ResourceType,
        resource_id: uuid.UUID,
        principal_type: PrincipalType,
        principal_id: uuid.UUID,
        permission: Permission | None = None,
    ) -> int:
        """`permission=None` revokes every permission this principal has on this resource."""
        conditions = [
            ResourcePermission.resource_type == resource_type,
            ResourcePermission.resource_id == resource_id,
            ResourcePermission.principal_type == principal_type,
            ResourcePermission.principal_id == principal_id,
        ]
        if permission is not None:
            conditions.append(ResourcePermission.permission == permission)
        result = await self._session.execute(delete(ResourcePermission).where(*conditions))
        await self.flush()
        return result.rowcount

    async def granted_permissions(
        self,
        *,
        organization_id: uuid.UUID,
        resource_type: ResourceType,
        resource_ids: list[uuid.UUID],
        principal_ids: list[uuid.UUID],
    ) -> set[Permission]:
        """
        The union of every `Permission` granted on ANY of `resource_ids`
        (a resource plus its ancestor folders — see
        `app/core/authorization.py`) to ANY of `principal_ids` (the
        user themself plus their group memberships). Both lists are
        small and bounded (a folder depth and a user's group count are
        not attacker-controlled, unbounded inputs), so one `IN (...)`
        query per check is appropriate — no batching/caching added
        beyond what `PermissionResolver`'s own caller already does.
        """
        if not resource_ids or not principal_ids:
            return set()
        result = await self._session.execute(
            select(ResourcePermission.permission).where(
                ResourcePermission.organization_id == organization_id,
                ResourcePermission.resource_type == resource_type,
                ResourcePermission.resource_id.in_(resource_ids),
                ResourcePermission.principal_id.in_(principal_ids),
            )
        )
        return set(result.scalars().all())

    async def list_for_resource(self, resource_type: ResourceType, resource_id: uuid.UUID) -> list[ResourcePermission]:
        result = await self._session.execute(
            select(ResourcePermission).where(
                ResourcePermission.resource_type == resource_type,
                ResourcePermission.resource_id == resource_id,
            )
        )
        return list(result.scalars().all())
