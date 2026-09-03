"""RefreshToken repository — persistence for refresh-token rotation/revocation."""

import uuid
from datetime import datetime

from sqlalchemy import select, update
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

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        """
        Revokes every non-revoked refresh token belonging to `user_id`
        in a single statement (Phase 10).

        The reuse-detection path in `AuthService.refresh` calls this the
        moment an already-rotated (revoked) `jti` is presented again —
        a strong signal the token was stolen and replayed. Revoking
        every session, not just the one that was replayed, is
        deliberate: whoever replayed the stolen token may hold others
        from the same theft, and there is no way from here to tell
        which of the user's other live sessions are the legitimate
        holder's versus the attacker's. Forcing a full re-login
        everywhere is the safe default; a device-tracking feature that
        could narrow the blast radius further does not exist yet (see
        `docs/security/authentication.md`).

        A single `UPDATE ... WHERE` (not a fetch-then-loop-then-flush)
        so this is one round trip regardless of how many sessions the
        user has open, and so it is correct even if this method races
        against a concurrent `revoke()` of one specific token — both
        converge on `revoked = true`, which is idempotent.

        Returns the number of rows actually flipped (0 if the user had
        no other live sessions), for the audit `detail` payload.
        """
        result = await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False))
            .values(revoked=True)
        )
        await self._session.flush()
        return result.rowcount or 0
