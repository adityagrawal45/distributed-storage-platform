from fastapi import APIRouter, Depends, status
from app.services.auth_service import AuthService
from app.api.dependencies import get_auth_service
from app.domain.schemas import UserCreate, LoginRequest, RefreshTokenRequest
from app.utils.response import success_response

router = APIRouter(prefix="/auth", tags=["Auth"])

@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    user_data: UserCreate,
    auth_service: AuthService = Depends(get_auth_service),
):
    tokens = await auth_service.register_user(user_data)
    return success_response(message="User registered successfully", data=tokens.model_dump())


@router.post("/login")
async def login(
    login_request: LoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    tokens = await auth_service.login_user(login_request)
    return success_response(message="Login successful", data=tokens.model_dump())


@router.post("/refresh")
async def refresh_token(
    refresh_request: RefreshTokenRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    tokens = await auth_service.refresh_access_token(refresh_request.refresh_token)
    return success_response(message="Token refreshed successfully", data=tokens.model_dump())
