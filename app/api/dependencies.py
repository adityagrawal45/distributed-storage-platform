from typing import Annotated
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis
from app.infrastructure.database import get_db
from app.infrastructure.redis_client import get_redis
from app.core.security import decode_token
from app.core.exceptions import AuthenticationError, AuthorizationError
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.domain.schemas import UserResponse
from app.core.enums import Role


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    # Extract token from Authorization header
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise AuthenticationError("Missing or invalid authorization header")
    token = auth_header.split(" ")[1]
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise AuthenticationError("Invalid token type")
    user_uuid = payload.get("sub")
    if not user_uuid:
        raise AuthenticationError("Invalid token payload")

    service = UserService(db)
    user = await service.get_user_by_uuid(user_uuid)
    if not user:
        raise AuthenticationError("User not found")
    return user


async def get_current_active_user(
    current_user: Annotated[UserResponse, Depends(get_current_user)]
) -> UserResponse:
    if not current_user.is_active:
        raise AuthenticationError("Inactive user")
    return current_user


async def get_current_admin_user(
    current_user: Annotated[UserResponse, Depends(get_current_active_user)]
) -> UserResponse:
    if current_user.role != Role.ADMIN:
        raise AuthorizationError("Admin privileges required")
    return current_user


# For service injection
async def get_auth_service(db: AsyncSession = Depends(get_db)) -> AuthService:
    return AuthService(db)


async def get_user_service(db: AsyncSession = Depends(get_db)) -> UserService:
    return UserService(db)


async def get_redis_client() -> Redis:
    return await get_redis()