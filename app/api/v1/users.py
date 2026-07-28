from fastapi import APIRouter, Depends
from app.api.dependencies import get_current_user, get_current_admin_user
from app.domain.schemas import UserResponse
from app.utils.response import success_response

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("/me")
async def get_me(current_user: UserResponse = Depends(get_current_user)):
    """Get current user profile."""
    return success_response(data=current_user.model_dump())


@router.get("/admin-only")
async def admin_only(current_admin: UserResponse = Depends(get_current_admin_user)):
    """Admin-only endpoint."""
    return success_response(message="Admin access granted", data={"user": current_admin.model_dump()})