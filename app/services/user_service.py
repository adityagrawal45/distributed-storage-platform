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