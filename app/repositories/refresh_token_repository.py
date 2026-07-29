"""RefreshToken repository — persistence for refresh-token rotation/revocation."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.refresh_token import RefreshToken
from app.repositories.base import BaseRepository


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    model = RefreshToken

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_jti(self, jti: uuid.UUID) -> RefreshToken | None:
        result = await self._session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
        return result.scalar_one_or_none()

    async def create(self, user_id: uuid.UUID, jti: uuid.UUID, expires_at: datetime) -> RefreshToken:
        token = RefreshToken(user_id=user_id, jti=jti, expires_at=expires_at)
        return await self.add(token)

    async def revoke(self, token: RefreshToken) -> None:
        token.revoked = True
        await self._session.flush()
