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