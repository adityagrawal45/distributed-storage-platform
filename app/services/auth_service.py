<<<<<<< HEAD
"""
Authentication service — all business logic for registration, login,
token refresh, and logout lives here (never in route handlers).

Design decisions:
- Refresh Token Rotation: every time `/auth/refresh` is called, the
  presented refresh token's `jti` is validated against the DB, then
  immediately revoked, and a brand-new access+refresh pair is issued.
  This limits the blast radius of a leaked refresh token to a single
  use — a replayed (already-used) refresh token is detected because its
  `jti` is marked `revoked=True` in the database.
- Logout Design: logout revokes the specific refresh token's `jti`. The
  short-lived access token cannot be revoked server-side (JWTs are
  stateless by design) — it simply expires naturally within
  `ACCESS_TOKEN_EXPIRE_MINUTES`. This is a standard, accepted trade-off;
  keeping access tokens short-lived is what bounds the exposure window.
- Password verification failures and "user not found" both raise the
  same `InvalidCredentialsException` with an identical message, to
  avoid user-enumeration via response differences (timing side-channels
  are a separate, deferred concern).
"""

import uuid

from app.core.security.password import hash_password, verify_password
from app.core.security.tokens import TokenType, create_access_token, create_refresh_token, decode_token
from app.exceptions.custom_exceptions import (
    EmailAlreadyExistsException,
    InactiveUserException,
    InvalidCredentialsException,
    InvalidTokenException,
)
from app.models.user import User
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import TokenPair
from app.schemas.user import UserCreate
from app.core.config import get_settings

settings = get_settings()


class AuthService:
    def __init__(self, user_repository: UserRepository, refresh_token_repository: RefreshTokenRepository):
        self._users = user_repository
        self._refresh_tokens = refresh_token_repository

    async def register(self, payload: UserCreate) -> User:
        if await self._users.email_exists(payload.email):
            raise EmailAlreadyExistsException()

        user = User(
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email,
            hashed_password=hash_password(payload.password),
        )
        return await self._users.add(user)

    async def _issue_token_pair(self, user: User) -> TokenPair:
        access_token = create_access_token(user_id=user.id, role=user.role.value)
        refresh_token, jti, expires_at = create_refresh_token(user_id=user.id)
        await self._refresh_tokens.create(user_id=user.id, jti=uuid.UUID(jti), expires_at=expires_at)

        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    async def login(self, email: str, password: str) -> TokenPair:
        user = await self._users.get_by_email(email)
        if user is None or not verify_password(password, user.hashed_password):
            raise InvalidCredentialsException()
        if not user.is_active:
            raise InactiveUserException()

        return await self._issue_token_pair(user)

    async def refresh(self, refresh_token: str) -> TokenPair:
        payload = decode_token(refresh_token, expected_type=TokenType.REFRESH)
        jti = uuid.UUID(payload["jti"])
        user_id = uuid.UUID(payload["sub"])

        stored = await self._refresh_tokens.get_by_jti(jti)
        if stored is None or stored.revoked or stored.user_id != user_id:
            raise InvalidTokenException(detail="Refresh token has been revoked or is invalid.")

        user = await self._users.get_by_id(user_id)
        if user is None or not user.is_active:
            raise InactiveUserException()

        # Rotation: burn the presented refresh token before issuing a new pair.
        await self._refresh_tokens.revoke(stored)
        return await self._issue_token_pair(user)

    async def logout(self, refresh_token: str) -> None:
        payload = decode_token(refresh_token, expected_type=TokenType.REFRESH)
        jti = uuid.UUID(payload["jti"])
        stored = await self._refresh_tokens.get_by_jti(jti)
        if stored is not None and not stored.revoked:
            await self._refresh_tokens.revoke(stored)
=======
from sqlalchemy.ext.asyncio import AsyncSession
from app.infrastructure.repositories.user_repository import UserRepository
from app.domain.schemas import UserCreate, LoginRequest, TokenResponse
from app.core.security import verify_password, create_tokens, decode_token
from app.core.exceptions import AuthenticationError, ConflictError
from app.core.enums import Role


class AuthService:
    def __init__(self, session: AsyncSession):
        self.user_repo = UserRepository(session)

    async def register_user(self, user_data: UserCreate) -> TokenResponse:
        # Check if user exists
        existing = await self.user_repo.get_by_email(user_data.email)
        if existing:
            raise ConflictError("User with this email already exists")

        user = await self.user_repo.create_user(user_data)
        access_token, refresh_token = create_tokens(str(user.uuid), user.role)
        return TokenResponse(access_token=access_token, refresh_token=refresh_token)

    async def login_user(self, login_data: LoginRequest) -> TokenResponse:
        user = await self.user_repo.get_by_email(login_data.email)
        if not user or not verify_password(login_data.password, user.hashed_password):
            raise AuthenticationError("Invalid email or password")
        if not user.is_active:
            raise AuthenticationError("User account is inactive")

        access_token, refresh_token = create_tokens(str(user.uuid), user.role)
        return TokenResponse(access_token=access_token, refresh_token=refresh_token)

    async def refresh_access_token(self, refresh_token: str) -> TokenResponse:
        payload = decode_token(refresh_token)
        if payload.get("type") != "refresh":
            raise AuthenticationError("Invalid token type")
        user_uuid = payload.get("sub")
        if not user_uuid:
            raise AuthenticationError("Invalid token payload")

        # Optionally check if refresh token is blacklisted (future)

        # Generate new tokens (optional: rotate refresh token)
        user = await self.user_repo.get_by_uuid(user_uuid)
        if not user or not user.is_active:
            raise AuthenticationError("User not found or inactive")

        access_token, new_refresh_token = create_tokens(str(user.uuid), user.role)
        return TokenResponse(access_token=access_token, refresh_token=new_refresh_token)
>>>>>>> b62d862acc4e93e3c4a06e1dd0022682031f3115
