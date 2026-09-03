"""
User routes.

Demonstrates:
- `/users/me`: any authenticated user (protected route via `CurrentUser`).
- `/users/{user_id}`: admin-only (role-based authorization via
  `require_role`).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.enums import AuditEventType, AuditResult
from app.dependencies.auth import CurrentUser, require_role
from app.dependencies.providers import AuditServiceDep, UserServiceDep
from app.models.user import User, UserRole
from app.schemas.response import APIResponse
from app.schemas.user import UserRead

router = APIRouter(prefix="/users", tags=["Users"])


@router.get(
    "/me",
    response_model=APIResponse[UserRead],
    summary="Get the currently authenticated user's profile",
)
async def get_my_profile(current_user: CurrentUser) -> APIResponse[UserRead]:
    return APIResponse(message="Profile retrieved successfully.", data=UserRead.model_validate(current_user))


@router.get(
    "/{user_id}",
    response_model=APIResponse[UserRead],
    summary="Get a user by ID (admin only)",
)
async def get_user_by_id(
    user_id: uuid.UUID,
    user_service: UserServiceDep,
    audit_service: AuditServiceDep,
    request: Request,
    _admin: Annotated[User, Depends(require_role(UserRole.ADMIN))],
) -> APIResponse[UserRead]:
    """Restricted to ADMIN role via the `require_role` dependency factory."""
    # Phase 7: cache-aside. The ADMIN check above still runs on every
    # request — the cache holds the user resource, never the authorization
    # decision (see UserService's module docstring).
    profile = await user_service.get_profile(user_id)
    # Phase 10: an admin looking up ANOTHER user's profile is exactly
    # the kind of privileged access the audit trail exists to make
    # reviewable after the fact — recorded regardless of whether
    # `user_id` happens to equal the admin's own ID.
    await audit_service.record(
        AuditEventType.ADMIN_ACTION,
        result=AuditResult.SUCCESS,
        actor_user_id=_admin.id,
        resource_type="user",
        resource_id=user_id,
        ip_address=getattr(request.state, "client_ip", None),
        detail={"action": "get_user_by_id"},
    )
    return APIResponse(message="User retrieved successfully.", data=profile)
