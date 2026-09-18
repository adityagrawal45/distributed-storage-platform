"""
`OrganizationService` — organization lifecycle (Phase 13 §7, §24-25).

Platform admin vs. organization admin (Phase 13 §25)
------------------------------------------------------
`create` (a brand-new, non-personal organization) and `suspend`/
`reactivate` are PLATFORM-level operations — gated by `UserRole.ADMIN`
(the pre-existing PLATFORM role from Phase 1), never by an
`OrganizationRole`. An organization's own OWNER cannot suspend their
own organization, and a platform admin's authority is not scoped to
any one tenant — this is the actual enforcement of "a tenant admin
should only control their own tenant" (Phase 13 §25): the capability
to affect OTHER organizations simply does not exist on the
`OrganizationRole` side of the authorization model at all, not because
of an extra check, but because these methods are only ever reachable
from routes gated by `require_role(UserRole.ADMIN)`
(`app/dependencies/auth.py`, unchanged from Phase 1) — see
`app/api/v1/organizations/routes.py`.
"""

from __future__ import annotations

import re
import uuid

from app.core.config import get_settings
from app.core.enums import AuditEventType, AuditResult, OrganizationRole, OrganizationStatus
from app.exceptions.custom_exceptions import OrganizationNotFoundException
from app.logging.logger import get_logger
from app.models.organization import Organization
from app.models.organization_membership import OrganizationMembership
from app.repositories.membership_repository import MembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.services.audit_service import AuditService

logger = get_logger(__name__)

_SLUG_SANITIZE = re.compile(r"[^a-z0-9-]+")


def slugify(name: str, suffix: str) -> str:
    """`suffix` (e.g. the first 8 hex chars of a fresh UUID) guarantees uniqueness without a
    retry-on-collision loop — see `OrganizationService.create`."""
    base = _SLUG_SANITIZE.sub("-", name.strip().lower()).strip("-") or "org"
    return f"{base[:80]}-{suffix}"


class OrganizationService:
    def __init__(
        self,
        organization_repository: OrganizationRepository,
        membership_repository: MembershipRepository,
        *,
        audit: AuditService | None = None,
    ):
        self._organizations = organization_repository
        self._memberships = membership_repository
        self._audit = audit

    async def create_personal(self, owner_user_id: uuid.UUID, display_name: str) -> Organization:
        """
        Called exactly once per user, at registration (`AuthService.register`,
        Phase 13's backward-compatibility mechanism — see
        `docs/multi-tenancy.md` "Migration strategy"). Personal
        organizations have NO storage limit by default (`Organization.
        storage_limit_bytes=NULL`) — a solo user's experience is meant
        to be identical to before Phase 13 existed; quotas are an
        opt-in concept an OWNER can set on their own organization later
        (see `OrganizationService.update_quota`), not something silently
        imposed on every existing user by this migration.
        """
        org = Organization(
            name=f"{display_name}'s Organization",
            slug=slugify(display_name, uuid.uuid4().hex[:8]),
            is_personal=True,
            storage_limit_bytes=None,
        )
        org = await self._organizations.add(org)
        membership = OrganizationMembership(
            organization_id=org.id, user_id=owner_user_id, role=OrganizationRole.OWNER
        )
        await self._memberships.add(membership)
        return org

    async def create(self, actor_user_id: uuid.UUID, name: str) -> Organization:
        """Platform-admin-only (Phase 13 §25) — see this module's docstring."""
        settings = get_settings()
        org = Organization(
            name=name,
            slug=slugify(name, uuid.uuid4().hex[:8]),
            is_personal=False,
            storage_limit_bytes=settings.ORG_DEFAULT_STORAGE_LIMIT_BYTES,
        )
        org = await self._organizations.add(org)
        membership = OrganizationMembership(organization_id=org.id, user_id=actor_user_id, role=OrganizationRole.OWNER)
        await self._memberships.add(membership)

        if self._audit is not None:
            await self._audit.record(
                AuditEventType.ORGANIZATION_CREATED,
                result=AuditResult.SUCCESS,
                actor_user_id=actor_user_id,
                organization_id=org.id,
                resource_type="organization",
                resource_id=org.id,
            )
        return org

    async def get(self, organization_id: uuid.UUID) -> Organization:
        org = await self._organizations.get_by_id(organization_id)
        if org is None:
            raise OrganizationNotFoundException()
        return org

    async def set_status(
        self, actor_user_id: uuid.UUID, organization_id: uuid.UUID, status: OrganizationStatus
    ) -> Organization:
        """Platform-admin-only. `status=SUSPENDED` blocks all data-plane access — see
        `app/dependencies/organization.py`'s membership resolution, which checks this."""
        org = await self.get(organization_id)
        await self._organizations.set_status(organization_id, status)
        org.status = status

        if self._audit is not None:
            event = (
                AuditEventType.ORGANIZATION_SUSPENDED
                if status == OrganizationStatus.SUSPENDED
                else AuditEventType.ORGANIZATION_REACTIVATED
            )
            await self._audit.record(
                event,
                result=AuditResult.SUCCESS,
                actor_user_id=actor_user_id,
                organization_id=organization_id,
                resource_type="organization",
                resource_id=organization_id,
            )
        logger.info("organization_status_changed", organization_id=str(organization_id), status=status.value)
        return org

    async def update_quota(
        self, actor_user_id: uuid.UUID, organization_id: uuid.UUID, *, storage_limit_bytes: int | None
    ) -> Organization:
        org = await self.get(organization_id)
        previous = org.storage_limit_bytes
        org.storage_limit_bytes = storage_limit_bytes
        await self._organizations.flush()

        if self._audit is not None:
            await self._audit.record(
                AuditEventType.QUOTA_CHANGED,
                result=AuditResult.SUCCESS,
                actor_user_id=actor_user_id,
                organization_id=organization_id,
                resource_type="organization",
                resource_id=organization_id,
                detail={"previous_limit_bytes": previous, "new_limit_bytes": storage_limit_bytes},
            )
        return org
