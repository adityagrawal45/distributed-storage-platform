<<<<<<< HEAD
"""User service — business logic for user retrieval/management."""

import uuid

from app.exceptions.custom_exceptions import NotFoundException
from app.models.user import User
from app.repositories.user_repository import UserRepository


class UserService:
    def __init__(self, user_repository: UserRepository):
        self._users = user_repository

    async def get_by_id(self, user_id: uuid.UUID) -> User:
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise NotFoundException(detail="User not found.")
        return user
=======
from sqlalchemy.ext.asyncio import AsyncSession
from app.infrastructure.repositories.user_repository import UserRepository
from app.domain.schemas import UserResponse
from app.core.exceptions import NotFoundError


class UserService:
    def __init__(self, session: AsyncSession):
        self.user_repo = UserRepository(session)

    async def get_user_by_uuid(self, user_uuid: str) -> UserResponse:
        user = await self.user_repo.get_by_uuid(user_uuid)
        if not user:
            raise NotFoundError("User not found")
        return UserResponse.model_validate(user)
>>>>>>> b62d862acc4e93e3c4a06e1dd0022682031f3115
