"""
User routes.

Demonstrates:
- `/users/me`: any authenticated user (protected route via `CurrentUser`).
- `/users/{user_id}`: admin-only (role-based authorization via
  `require_role`).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies.auth import CurrentUser, require_role
from app.dependencies.providers import UserServiceDep
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
    _admin: Annotated[User, Depends(require_role(UserRole.ADMIN))],
) -> APIResponse[UserRead]:
    """Restricted to ADMIN role via the `require_role` dependency factory."""
    user = await user_service.get_by_id(user_id)
    return APIResponse(message="User retrieved successfully.", data=UserRead.model_validate(user))
