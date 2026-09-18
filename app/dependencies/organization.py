"""
Tenant-context resolution (Phase 13 §6).

`CurrentOrganization` is the SECOND dependency (after `CurrentUser`,
Phase 1) every organization-scoped route depends on. It answers "which
tenant is this request acting within?" — and, per Phase 13 §2's
explicit instruction, that answer is NEVER taken from a client-supplied
`organization_id` in a request body/query string used as-is. It comes
from `X-Organization-ID` (a header, deliberately — not a body field a
Pydantic schema might accidentally forward straight into a query
filter) cross-checked against the AUTHENTICATED user's own active
memberships, resolved server-side by this dependency BEFORE any
handler body runs.

Why a header, and why membership belongs to a multi-org user matters
-----------------------------------------------------------------------
A user with exactly one organization (every user, by default — see
`OrganizationService.create_personal`) never needs to send the header
at all: omitting it resolves unambiguously to that one membership,
which is what keeps every pre-Phase-13 API call (no client anywhere
sends `X-Organization-ID` yet) working unchanged (Phase 13 §40).
A user who belongs to MORE than one organization (joined a team, or a
platform admin's own account) MUST send the header — there is no
"last used" session-sticky default, deliberately: silently picking one
of several organizations for an ambiguous request is exactly the kind
of implicit tenant-switching Phase 13 §6 says not to build.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header

from app.core.enums import OrganizationRole, OrganizationStatus
from app.dependencies.auth import CurrentUser
from app.dependencies.providers import MembershipRepositoryDep, OrganizationRepositoryDep
from app.exceptions.custom_exceptions import (
    NotOrganizationMemberException,
    OrganizationNotFoundException,
    OrganizationSuspendedException,
    ValidationException,
)
from app.models.organization import Organization
from app.models.organization_membership import OrganizationMembership


@dataclass(frozen=True)
class OrganizationContext:
    organization: Organization
    membership: OrganizationMembership

    @property
    def organization_id(self) -> uuid.UUID:
        return self.organization.id

    @property
    def role(self) -> OrganizationRole:
        return self.membership.role


async def get_current_organization(
    current_user: CurrentUser,
    organizations: OrganizationRepositoryDep,
    memberships: MembershipRepositoryDep,
    x_organization_id: Annotated[str | None, Header(alias="X-Organization-ID")] = None,
) -> OrganizationContext:
    if x_organization_id is not None:
        try:
            organization_id = uuid.UUID(x_organization_id)
        except ValueError as exc:
            raise ValidationException("X-Organization-ID must be a valid UUID.") from exc

        membership = await memberships.get_active(current_user.id, organization_id)
        if membership is None:
            raise NotOrganizationMemberException()
    else:
        active_memberships = await memberships.list_for_user(current_user.id)
        if not active_memberships:
            # Should not happen post-registration (every user gets a
            # personal org) — defensive, not a code path any current
            # flow reaches.
            raise NotOrganizationMemberException(detail="You do not belong to any organization.")
        if len(active_memberships) > 1:
            raise ValidationException(
                "You belong to multiple organizations — specify one via the X-Organization-ID header."
            )
        membership = active_memberships[0]
        organization_id = membership.organization_id

    organization = await organizations.get_by_id(organization_id)
    if organization is None:
        raise OrganizationNotFoundException()
    if organization.status == OrganizationStatus.SUSPENDED:
        raise OrganizationSuspendedException()
    if organization.status == OrganizationStatus.DELETED:
        # Deliberately the SAME exception as "not a member" (Phase 13
        # §16-style non-disclosure reasoning applied here too): a
        # deleted organization should look, to anyone still holding an
        # old token, exactly like one they never belonged to.
        raise NotOrganizationMemberException()

    return OrganizationContext(organization=organization, membership=membership)


CurrentOrganization = Annotated[OrganizationContext, Depends(get_current_organization)]


def require_org_role(*allowed_roles: OrganizationRole):
    """Dependency factory mirroring `app/dependencies/auth.py::require_role` — same shape, one level down
    (organization role instead of platform role)."""

    async def _dependency(context: CurrentOrganization) -> OrganizationContext:
        if context.role not in allowed_roles:
            raise NotOrganizationMemberException(
                detail=f"This action requires one of the following organization roles: "
                f"{', '.join(r.value for r in allowed_roles)}."
            )
        return context

    return _dependency
