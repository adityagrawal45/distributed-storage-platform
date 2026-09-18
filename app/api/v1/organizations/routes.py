"""
Organization administration (Phase 13 §7-13, §24-28).

Two distinct authority levels are enforced on THIS router, never
blurred (see `OrganizationService`'s module docstring):
- Platform-level operations (`create`, `suspend`/`reactivate` any
  organization) are gated by `require_role(UserRole.ADMIN)` — the
  pre-existing Phase 1 platform role.
- Organization-level operations (members, groups, permission grants,
  quota, audit search) are gated by `CurrentOrganization` +
  `require_org_role(...)` — scoped to the caller's OWN organization,
  resolved server-side, never a client-supplied org ID for a DIFFERENT
  tenant (Phase 13 §2/§6).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.enums import OrganizationRole, OrganizationStatus, Permission, ResourceType
from app.dependencies.auth import CurrentUser, require_role
from app.dependencies.organization import CurrentOrganization, OrganizationContext, require_org_role
from app.dependencies.providers import (
    AuditLogRepositoryDep,
    FileMetadataRepositoryDep,
    FolderRepositoryDep,
    GroupServiceDep,
    MembershipServiceDep,
    OrganizationServiceDep,
    PermissionResolverDep,
    PermissionServiceDep,
)
from app.exceptions.custom_exceptions import FileNotFoundException, FolderNotFoundException
from app.models.file_metadata import FileMetadata
from app.models.folder import Folder
from app.models.user import User, UserRole
from app.schemas.organization import (
    AuditLogRead,
    GroupCreate,
    GroupMemberAdd,
    GroupRead,
    MemberAdd,
    MemberRoleChange,
    MembershipRead,
    OrganizationCreate,
    OrganizationQuotaUpdate,
    OrganizationRead,
)
from app.schemas.pagination import Page, PaginationParams
from app.schemas.permission import PermissionGrantCreate, PermissionRevoke, ResourcePermissionRead
from app.schemas.response import APIResponse

router = APIRouter(prefix="/organizations", tags=["Organizations"])


async def _load_resource(
    resource_type: ResourceType,
    resource_id: uuid.UUID,
    organization_id: uuid.UUID,
    folders: FolderRepositoryDep,
    files: FileMetadataRepositoryDep,
) -> Folder | FileMetadata:
    if resource_type == ResourceType.FOLDER:
        folder = await folders.get_in_organization(resource_id, organization_id)
        if folder is None:
            raise FolderNotFoundException()
        return folder
    file = await files.get_in_organization(resource_id, organization_id)
    if file is None:
        raise FileNotFoundException()
    return file


# ----------------------------------------------------------------------
# Platform-admin: organization lifecycle
# ----------------------------------------------------------------------
@router.post(
    "",
    response_model=APIResponse[OrganizationRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create a new organization (platform admin only)",
)
async def create_organization(
    payload: OrganizationCreate,
    organization_service: OrganizationServiceDep,
    admin: Annotated[User, Depends(require_role(UserRole.ADMIN))],
) -> APIResponse[OrganizationRead]:
    org = await organization_service.create(admin.id, payload.name)
    return APIResponse(message="Organization created successfully.", data=OrganizationRead.model_validate(org))


@router.post(
    "/{organization_id}/suspend",
    response_model=APIResponse[OrganizationRead],
    summary="Suspend an organization, blocking all data-plane access (platform admin only)",
)
async def suspend_organization(
    organization_id: uuid.UUID,
    organization_service: OrganizationServiceDep,
    admin: Annotated[User, Depends(require_role(UserRole.ADMIN))],
) -> APIResponse[OrganizationRead]:
    org = await organization_service.set_status(admin.id, organization_id, OrganizationStatus.SUSPENDED)
    return APIResponse(message="Organization suspended.", data=OrganizationRead.model_validate(org))


@router.post(
    "/{organization_id}/reactivate",
    response_model=APIResponse[OrganizationRead],
    summary="Reactivate a suspended organization (platform admin only)",
)
async def reactivate_organization(
    organization_id: uuid.UUID,
    organization_service: OrganizationServiceDep,
    admin: Annotated[User, Depends(require_role(UserRole.ADMIN))],
) -> APIResponse[OrganizationRead]:
    org = await organization_service.set_status(admin.id, organization_id, OrganizationStatus.ACTIVE)
    return APIResponse(message="Organization reactivated.", data=OrganizationRead.model_validate(org))


# ----------------------------------------------------------------------
# Org-scoped: the caller's own organization
# ----------------------------------------------------------------------
@router.get("/me", response_model=APIResponse[OrganizationRead], summary="Get the organization you're currently acting in")
async def get_current_organization_details(org: CurrentOrganization) -> APIResponse[OrganizationRead]:
    return APIResponse(message="Organization retrieved successfully.", data=OrganizationRead.model_validate(org.organization))


@router.put(
    "/me/quota",
    response_model=APIResponse[OrganizationRead],
    summary="Update this organization's storage quota (OWNER/ADMIN only)",
)
async def update_quota(
    payload: OrganizationQuotaUpdate,
    current_user: CurrentUser,
    organization_service: OrganizationServiceDep,
    org: Annotated[OrganizationContext, Depends(require_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN))],
) -> APIResponse[OrganizationRead]:
    updated = await organization_service.update_quota(
        current_user.id, org.organization_id, storage_limit_bytes=payload.storage_limit_bytes
    )
    return APIResponse(message="Quota updated successfully.", data=OrganizationRead.model_validate(updated))


@router.get("/me/members", response_model=APIResponse[Page[MembershipRead]], summary="List this organization's members")
async def list_members(
    org: CurrentOrganization,
    membership_service: MembershipServiceDep,
    pagination: Annotated[PaginationParams, Depends()],
) -> APIResponse[Page[MembershipRead]]:
    members, total = await membership_service.list_members(
        org.organization_id, limit=pagination.page_size, offset=pagination.offset
    )
    page = Page.create(
        items=[MembershipRead.model_validate(m) for m in members],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )
    return APIResponse(message="Members retrieved successfully.", data=page)


@router.post(
    "/me/members",
    response_model=APIResponse[MembershipRead],
    status_code=status.HTTP_201_CREATED,
    summary="Add an existing NimbusFS user to this organization (OWNER/ADMIN only)",
)
async def add_member(
    payload: MemberAdd,
    current_user: CurrentUser,
    membership_service: MembershipServiceDep,
    org: Annotated[OrganizationContext, Depends(require_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN))],
) -> APIResponse[MembershipRead]:
    membership = await membership_service.add_member(current_user.id, org.organization_id, payload.email, payload.role)
    return APIResponse(message="Member added successfully.", data=MembershipRead.model_validate(membership))


@router.put(
    "/me/members/{member_user_id}/role",
    response_model=APIResponse[MembershipRead],
    summary="Change a member's role (OWNER/ADMIN only)",
)
async def change_member_role(
    member_user_id: uuid.UUID,
    payload: MemberRoleChange,
    current_user: CurrentUser,
    membership_service: MembershipServiceDep,
    org: Annotated[OrganizationContext, Depends(require_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN))],
) -> APIResponse[MembershipRead]:
    membership = await membership_service.change_role(
        current_user.id, org.organization_id, member_user_id, payload.role
    )
    return APIResponse(message="Member role updated successfully.", data=MembershipRead.model_validate(membership))


@router.delete(
    "/me/members/{member_user_id}",
    response_model=APIResponse[None],
    summary="Remove a member from this organization (OWNER/ADMIN only)",
)
async def remove_member(
    member_user_id: uuid.UUID,
    current_user: CurrentUser,
    membership_service: MembershipServiceDep,
    org: Annotated[OrganizationContext, Depends(require_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN))],
) -> APIResponse[None]:
    await membership_service.remove_member(current_user.id, org.organization_id, member_user_id)
    return APIResponse(message="Member removed successfully.")


# ----------------------------------------------------------------------
# Groups
# ----------------------------------------------------------------------
@router.get("/me/groups", response_model=APIResponse[list[GroupRead]], summary="List this organization's groups")
async def list_groups(org: CurrentOrganization, group_service: GroupServiceDep) -> APIResponse[list[GroupRead]]:
    groups = await group_service.list_for_organization(org.organization_id)
    return APIResponse(message="Groups retrieved successfully.", data=[GroupRead.model_validate(g) for g in groups])


@router.post(
    "/me/groups",
    response_model=APIResponse[GroupRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create a group (OWNER/ADMIN only)",
)
async def create_group(
    payload: GroupCreate,
    group_service: GroupServiceDep,
    org: Annotated[OrganizationContext, Depends(require_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN))],
) -> APIResponse[GroupRead]:
    group = await group_service.create(org.organization_id, payload.name)
    return APIResponse(message="Group created successfully.", data=GroupRead.model_validate(group))


@router.get(
    "/me/groups/{group_id}/members",
    response_model=APIResponse[list[uuid.UUID]],
    summary="List a group's members",
)
async def list_group_members(
    group_id: uuid.UUID, org: CurrentOrganization, group_service: GroupServiceDep
) -> APIResponse[list[uuid.UUID]]:
    members = await group_service.list_members(group_id, org.organization_id)
    return APIResponse(message="Group members retrieved successfully.", data=[m.user_id for m in members])


@router.post(
    "/me/groups/{group_id}/members",
    response_model=APIResponse[None],
    status_code=status.HTTP_201_CREATED,
    summary="Add a member to a group (OWNER/ADMIN only)",
)
async def add_group_member(
    group_id: uuid.UUID,
    payload: GroupMemberAdd,
    group_service: GroupServiceDep,
    org: Annotated[OrganizationContext, Depends(require_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN))],
) -> APIResponse[None]:
    await group_service.add_member(group_id, org.organization_id, payload.user_id)
    return APIResponse(message="Group member added successfully.")


@router.delete(
    "/me/groups/{group_id}/members/{member_user_id}",
    response_model=APIResponse[None],
    summary="Remove a member from a group (OWNER/ADMIN only)",
)
async def remove_group_member(
    group_id: uuid.UUID,
    member_user_id: uuid.UUID,
    group_service: GroupServiceDep,
    org: Annotated[OrganizationContext, Depends(require_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN))],
) -> APIResponse[None]:
    await group_service.remove_member(group_id, org.organization_id, member_user_id)
    return APIResponse(message="Group member removed successfully.")


# ----------------------------------------------------------------------
# Resource permission grants (Phase 13 §11-13)
# ----------------------------------------------------------------------
@router.post(
    "/me/permissions",
    response_model=APIResponse[ResourcePermissionRead],
    status_code=status.HTTP_201_CREATED,
    summary="Grant a permission on a folder/file to a user or group",
)
async def grant_permission(
    payload: PermissionGrantCreate,
    current_user: CurrentUser,
    org: CurrentOrganization,
    permission_service: PermissionServiceDep,
    resolver: PermissionResolverDep,
    folders: FolderRepositoryDep,
    files: FileMetadataRepositoryDep,
) -> APIResponse[ResourcePermissionRead]:
    resource = await _load_resource(payload.resource_type, payload.resource_id, org.organization_id, folders, files)
    await resolver.require(
        user_id=current_user.id, membership=org.membership, resource=resource, permission=Permission.MANAGE
    )
    grant = await permission_service.grant(
        actor_user_id=current_user.id,
        organization_id=org.organization_id,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        principal_type=payload.principal_type,
        principal_id=payload.principal_id,
        permission=payload.permission,
    )
    return APIResponse(message="Permission granted successfully.", data=ResourcePermissionRead.model_validate(grant))


@router.post(
    "/me/permissions/revoke",
    response_model=APIResponse[None],
    summary="Revoke a permission (or all permissions, if `permission` is omitted) on a resource",
)
async def revoke_permission(
    payload: PermissionRevoke,
    current_user: CurrentUser,
    org: CurrentOrganization,
    permission_service: PermissionServiceDep,
    resolver: PermissionResolverDep,
    folders: FolderRepositoryDep,
    files: FileMetadataRepositoryDep,
) -> APIResponse[None]:
    resource = await _load_resource(payload.resource_type, payload.resource_id, org.organization_id, folders, files)
    await resolver.require(
        user_id=current_user.id, membership=org.membership, resource=resource, permission=Permission.MANAGE
    )
    await permission_service.revoke(
        actor_user_id=current_user.id,
        organization_id=org.organization_id,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        principal_type=payload.principal_type,
        principal_id=payload.principal_id,
        permission=payload.permission,
    )
    return APIResponse(message="Permission(s) revoked successfully.")


@router.get(
    "/me/permissions",
    response_model=APIResponse[list[ResourcePermissionRead]],
    summary="List permission grants on one resource",
)
async def list_permissions(
    current_user: CurrentUser,
    org: CurrentOrganization,
    permission_service: PermissionServiceDep,
    resolver: PermissionResolverDep,
    folders: FolderRepositoryDep,
    files: FileMetadataRepositoryDep,
    resource_type: ResourceType = Query(...),
    resource_id: uuid.UUID = Query(...),
) -> APIResponse[list[ResourcePermissionRead]]:
    resource = await _load_resource(resource_type, resource_id, org.organization_id, folders, files)
    await resolver.require(
        user_id=current_user.id, membership=org.membership, resource=resource, permission=Permission.MANAGE
    )
    grants = await permission_service.list_for_resource(resource_type, resource_id)
    return APIResponse(
        message="Permission grants retrieved successfully.", data=[ResourcePermissionRead.model_validate(g) for g in grants]
    )


# ----------------------------------------------------------------------
# Audit search (Phase 13 §27)
# ----------------------------------------------------------------------
@router.get(
    "/me/audit",
    response_model=APIResponse[Page[AuditLogRead]],
    summary="Search this organization's audit log (OWNER/ADMIN only)",
)
async def search_audit_log(
    audit_repository: AuditLogRepositoryDep,
    org: Annotated[OrganizationContext, Depends(require_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN))],
    pagination: Annotated[PaginationParams, Depends()],
) -> APIResponse[Page[AuditLogRead]]:
    entries, total = await audit_repository.search(
        organization_id=org.organization_id,
        limit=pagination.page_size,
        offset=pagination.offset,
    )
    page = Page.create(
        items=[AuditLogRead.model_validate(e) for e in entries],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )
    return APIResponse(message="Audit log retrieved successfully.", data=page)
