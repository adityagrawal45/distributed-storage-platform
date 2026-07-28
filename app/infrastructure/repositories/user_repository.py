from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.domain.models import User
from app.domain.schemas import UserCreate
from app.core.security import hash_password


class UserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_user(self, user_data: UserCreate) -> User:
        hashed = hash_password(user_data.password)
        db_user = User(
            email=user_data.email,
            first_name=user_data.first_name,
            last_name=user_data.last_name,
            hashed_password=hashed,
        )
        self.session.add(db_user)
        await self.session.commit()
        await self.session.refresh(db_user)
        return db_user

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.email == email)
        )
        return result.scalar_one_or_none()

    async def get_by_uuid(self, uuid: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.uuid == uuid)
        )
        return result.scalar_one_or_none()

    async def update_user(self, user: User) -> User:
        await self.session.commit()
        await self.session.refresh(user)
        return user