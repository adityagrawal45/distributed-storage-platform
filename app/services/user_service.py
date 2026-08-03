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
