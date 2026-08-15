"""
Authentication routes.

Design decision: handlers are intentionally thin — they parse input
(via Pydantic), call a single service method, and wrap the result in
`APIResponse`. All business logic (password verification, token
issuance/rotation, uniqueness checks) lives in `AuthService`. This keeps
routes trivially readable and lets the service layer be unit-tested
without spinning up HTTP.

Phase 7: `/register` and `/login` are the two most abuse-attractive
endpoints in the API (account-farming and credential-stuffing
respectively) and are also the only ones an unauthenticated caller can
reach, so they carry the tightest rate-limit budgets and are bucketed by
client IP rather than user ID (there is no user yet). See
`app/dependencies/rate_limit.py` for how identity is resolved and
`app/core/rate_limiter.py` for the algorithm.
"""

from fastapi import APIRouter, status
from fastapi.security import OAuth2PasswordRequestForm
from typing import Annotated

from fastapi import Depends

from app.core.rate_limiter import RateLimitCategory
from app.dependencies.providers import AuthServiceDep
from app.dependencies.rate_limit import rate_limit
from app.schemas.auth import RefreshTokenRequest, TokenPair
from app.schemas.response import APIResponse
from app.schemas.user import UserCreate, UserRead

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post(
    "/register",
    response_model=APIResponse[UserRead],
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user account",
    dependencies=[Depends(rate_limit(RateLimitCategory.REGISTER))],
)
async def register(payload: UserCreate, auth_service: AuthServiceDep) -> APIResponse[UserRead]:
    user = await auth_service.register(payload)
    return APIResponse(
        message="User registered successfully.",
        data=UserRead.model_validate(user),
    )


@router.post(
    "/login",
    response_model=APIResponse[TokenPair],
    summary="Authenticate and obtain an access/refresh token pair",
    dependencies=[Depends(rate_limit(RateLimitCategory.LOGIN))],
)
async def login(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    auth_service: AuthServiceDep,
) -> APIResponse[TokenPair]:
    # OAuth2PasswordRequestForm uses `username` for the identifier field;
    # we treat it as the user's email to stay OAuth2-spec-compliant while
    # keeping email-based login semantics.
    tokens = await auth_service.login(email=form_data.username, password=form_data.password)
    return APIResponse(message="Login successful.", data=tokens)


@router.post(
    "/refresh",
    response_model=APIResponse[TokenPair],
    summary="Exchange a refresh token for a new access/refresh token pair",
)
async def refresh(payload: RefreshTokenRequest, auth_service: AuthServiceDep) -> APIResponse[TokenPair]:
    tokens = await auth_service.refresh(payload.refresh_token)
    return APIResponse(message="Token refreshed successfully.", data=tokens)


@router.post(
    "/logout",
    response_model=APIResponse[None],
    summary="Revoke a refresh token (logout)",
)
async def logout(payload: RefreshTokenRequest, auth_service: AuthServiceDep) -> APIResponse[None]:
    await auth_service.logout(payload.refresh_token)
    return APIResponse(message="Logged out successfully.")
