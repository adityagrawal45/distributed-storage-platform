"""
`PermissionResolver` — the ONE authorization decision-maker for
resource-level access in NimbusFS (Phase 13 §13).

Why centralized, not per-endpoint `if` checks
----------------------------------------------
Phase 13 §13 is explicit: "Do not implement authorization logic
differently in different endpoints." Every route that touches a
folder/file that might not be owned by the caller (share redemption,
a future "shared with me" listing, an admin action) asks THIS class
the same question — "can `user`, acting in `organization`, do
`permission` on `resource`?" — and gets a deterministic yes/no. A
second, slightly-different authorization check written inline in a
route handler is exactly how a tenant-isolation bug gets introduced:
not by one big mistake, but by one endpoint quietly reimplementing
"is this mine?" a little differently than the others.

The precedence algorithm (Phase 13 §13, documented once, applied
everywhere)
------------------------------------------------------------------------
```
1. Organization membership          -> not a member at all? DENY, full stop.
2. Organization status              -> SUSPENDED?           DENY, full stop.
3. Resource ownership (owner_id)    -> owns it?              ALLOW (every permission).
4. Organization role: OWNER/ADMIN   -> ALLOW (every permission, org-wide).
5. Direct ResourcePermission grant  -> on the resource ITSELF -> ALLOW if it names this permission.
6. Inherited ResourcePermission     -> on an ANCESTOR FOLDER  -> ALLOW if it names this permission.
7. Otherwise                        -> DENY.
```

Step order matters and is fixed: ownership and OWNER/ADMIN role are
checked BEFORE touching `ResourcePermission` at all, because those two
are supposed to be unconditional — an owner is never one missing grant
row away from losing access to their own file. Everything past step 4
is the "was this explicitly shared with me" question, and it is
evaluated the same way for a MEMBER or a VIEWER (Phase 13 §9's table:
VIEWER's baseline is "no create, no delete, no share" — but an
explicit grant, e.g. `WRITE` on one shared folder, is a case §9's
table's "Policy" column exists for, and this resolver is where that
policy is actually applied, not re-decided per endpoint).

No deny list, no explicit-deny grants (Phase 13 §12 asks this to be
DEFINED, not necessarily built) — NimbusFS has no product requirement
yet for "block user X from a specific file they'd otherwise have
access to," and adding one now would be exactly the kind of
unrequested complexity Phase 13's own brief warns against (§10, §11).
Recorded as a real, deliberate scope limit in `docs/permissions.md`,
not a silent omission.

Fail-safe default
------------------
Every exit path that is not an explicit ALLOW is a DENY — there is no
code path in `check()` that returns "unknown" or raises past an
`AuthorizationException`. This is Phase 13 §44's "when authorization
state cannot be determined safely, prefer denying access" applied
structurally: a bug that makes a lookup fail (a DB error propagating
up) surfaces as a 5xx via the existing `SQLAlchemyError` handler, which
denies the request by definition (no 2xx response, no bytes served) —
it does not silently fall through to "allow".
"""

from __future__ import annotations

import uuid

from app.core.enums import OrganizationRole, Permission, ResourceType
from app.exceptions.custom_exceptions import AuthorizationException
from app.logging.logger import get_logger
from app.models.file_metadata import FileMetadata
from app.models.folder import Folder
from app.models.organization_membership import OrganizationMembership
from app.repositories.folder_repository import FolderRepository
from app.repositories.group_repository import GroupRepository
from app.repositories.resource_permission_repository import ResourcePermissionRepository

logger = get_logger(__name__)

#: OWNER/ADMIN bypass ResourcePermission entirely (step 4 above) — see this module's docstring.
_ORG_WIDE_ROLES = frozenset({OrganizationRole.OWNER, OrganizationRole.ADMIN})


def _ancestor_paths(path: str) -> list[str]:
    """
    Every ancestor path of `path`, INCLUDING `path` itself — e.g.
    `/A/B/C` -> `["/A", "/A/B", "/A/B/C"]`. A permission granted on any
    one of these folders cascades to a descendant (Phase 13 §12).
    Pure string splitting over the materialized path `Folder.path`
    already maintains (Phase 2) — no recursive query needed.
    """
    parts = [p for p in path.strip("/").split("/") if p]
    return ["/" + "/".join(parts[: i + 1]) for i in range(len(parts))]


class PermissionResolver:
    def __init__(
        self,
        folder_repository: FolderRepository,
        group_repository: GroupRepository,
        permission_repository: ResourcePermissionRepository,
    ):
        self._folders = folder_repository
        self._groups = group_repository
        self._permissions = permission_repository

    async def check(
        self,
        *,
        user_id: uuid.UUID,
        membership: OrganizationMembership,
        resource: Folder | FileMetadata,
        permission: Permission,
    ) -> bool:
        """Returns True/False — never raises. `require()` below is the raising counterpart routes actually use."""
        # Step 3: ownership.
        if resource.owner_id == user_id:
            return True

        # Step 4: org-wide roles.
        if membership.role in _ORG_WIDE_ROLES:
            return True

        # Steps 5-6: direct + inherited grants.
        resource_type = ResourceType.FOLDER if isinstance(resource, Folder) else ResourceType.FILE
        resource_ids = await self._grant_candidate_ids(resource, resource_type, membership.organization_id)

        group_ids = await self._groups.list_group_ids_for_user(user_id, membership.organization_id)
        principal_ids = [user_id, *group_ids]

        granted = await self._permissions.granted_permissions(
            organization_id=membership.organization_id,
            resource_type=resource_type,
            resource_ids=resource_ids,
            principal_ids=principal_ids,
        )
        # A grant of MANAGE implies every other permission on that
        # resource (Phase 13 §11: MANAGE is the "can administer this
        # resource" superset) — checked explicitly rather than
        # materializing MANAGE into 6 separate rows at grant time.
        if permission in granted or Permission.MANAGE in granted:
            return True

        # Folder-level grants also need checking against FOLDER-typed
        # ancestor IDs specifically when the resource itself is a FILE
        # (a file's own `resource_ids` above already includes its
        # folder ancestors' IDs as FOLDER-typed candidates — see
        # `_grant_candidate_ids` — but `granted_permissions` was just
        # called with `resource_type=resource_type` i.e. FILE for a
        # file's own direct grants. Re-check the FOLDER-typed ancestor
        # chain explicitly here.)
        if resource_type == ResourceType.FILE:
            assert isinstance(resource, FileMetadata)
            folder_ids = await self._folder_ancestor_ids(resource, membership.organization_id)
            if folder_ids:
                folder_grants = await self._permissions.granted_permissions(
                    organization_id=membership.organization_id,
                    resource_type=ResourceType.FOLDER,
                    resource_ids=folder_ids,
                    principal_ids=principal_ids,
                )
                if permission in folder_grants or Permission.MANAGE in folder_grants:
                    return True

        return False

    async def require(
        self,
        *,
        user_id: uuid.UUID,
        membership: OrganizationMembership,
        resource: Folder | FileMetadata,
        permission: Permission,
    ) -> None:
        """Raises `AuthorizationException` on denial — the form every route dependency actually calls."""
        allowed = await self.check(user_id=user_id, membership=membership, resource=resource, permission=permission)
        if not allowed:
            from app.core.metrics import AUTHORIZATION_DENIALS_TOTAL, safe_call

            safe_call(
                lambda: AUTHORIZATION_DENIALS_TOTAL.labels(reason="no_grant").inc(),
                operation="authorization_denials_total_inc",
            )
            logger.warning(
                "authorization_denied",
                permission=permission.value,
                resource_type="folder" if isinstance(resource, Folder) else "file",
                resource_id=str(resource.id),
                organization_id=str(membership.organization_id),
            )
            raise AuthorizationException(detail="You do not have permission to perform this action.")

    async def _grant_candidate_ids(
        self, resource: Folder | FileMetadata, resource_type: ResourceType, organization_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """For a FOLDER: itself plus every ancestor folder's ID (direct grant lookup covers inheritance
        in one query). For a FILE: only itself — its folder ancestors are FOLDER-typed rows, checked
        separately in `check()` above, since `resource_type` for this call is fixed to one type."""
        if resource_type == ResourceType.FOLDER:
            assert isinstance(resource, Folder)
            paths = _ancestor_paths(resource.path)
            return await self._folders.get_ancestor_folder_ids_by_path(organization_id, paths)
        return [resource.id]

    async def _folder_ancestor_ids(self, file: FileMetadata, organization_id: uuid.UUID) -> list[uuid.UUID]:
        if file.folder_id is None:
            return []
        folder = await self._folders.get_in_organization(file.folder_id, organization_id)
        if folder is None:
            return []
        paths = _ancestor_paths(folder.path)
        return await self._folders.get_ancestor_folder_ids_by_path(organization_id, paths)
