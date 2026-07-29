"""
Dependency injection providers.

Design decision: FastAPI's `Depends` graph IS our DI container. Each
provider function declares its own dependencies (e.g. `get_user_service`
depends on `get_db`), and FastAPI resolves the whole chain per-request.
This keeps constructors simple and explicit, and makes it trivial to
override any provider in tests via `app.dependency_overrides`.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.user_service import UserService

DbSession = Annotated[AsyncSession, Depends(get_db)]


def get_user_repository(session: DbSession) -> UserRepository:
    return UserRepository(session)


def get_refresh_token_repository(session: DbSession) -> RefreshTokenRepository:
    return RefreshTokenRepository(session)


UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]
RefreshTokenRepositoryDep = Annotated[RefreshTokenRepository, Depends(get_refresh_token_repository)]


def get_auth_service(
    user_repository: UserRepositoryDep,
    refresh_token_repository: RefreshTokenRepositoryDep,
) -> AuthService:
    return AuthService(user_repository, refresh_token_repository)


def get_user_service(user_repository: UserRepositoryDep) -> UserService:
    return UserService(user_repository)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
UserServiceDep = Annotated[UserService, Depends(get_user_service)]
