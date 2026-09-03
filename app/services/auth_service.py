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

Phase 10 additions (audit trail + refresh-token reuse hardening):
- `audit`, keyword-only, defaulting to `None` — the exact same
  backward-compatible pattern Phase 7 established for `cache=`/
  `invalidator=` and Phase 8 for `outbox=`. Every pre-existing
  construction of this service (including every prior test) keeps
  working unchanged and simply emits no audit trail; only the DI
  provider passes a real `AuditService`.
- `login`/`refresh`/`logout` now accept an optional `ip_address`
  keyword-only parameter, threaded from `request.state.client_ip`
  (populated by `TrustedProxyMiddleware`) at the route layer — see
  `app/api/v1/auth/routes.py`. Optional and defaulting to `None` for
  the same reason: nothing that already calls these methods without it
  breaks.
- `refresh` now distinguishes two failure shapes that were previously
  conflated into one generic "invalid or revoked" branch: a `jti` that
  is simply unknown/mismatched (garden-variety invalid token) versus a
  `jti` that IS known and IS already revoked (a refresh token being
  presented a second time — the direct signature of rotation-detected
  replay). The second case now revokes every other live session for
  that user and records a `TOKEN_REVOCATION` audit event — see
  `RefreshTokenRepository.revoke_all_for_user`'s docstring for why the
  blast radius is "every session," not just the replayed one.
"""

import uuid

from app.core.enums import AuditEventType, AuditResult
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
from app.services.audit_service import AuditService
from app.core.config import get_settings

settings = get_settings()


class AuthService:
    def __init__(
        self,
        user_repository: UserRepository,
        refresh_token_repository: RefreshTokenRepository,
        *,
        audit: AuditService | None = None,
    ):
        self._users = user_repository
        self._refresh_tokens = refresh_token_repository
        self._audit = audit

    async def _record_audit(
        self,
        event_type: AuditEventType,
        *,
        result: AuditResult,
        actor_user_id: uuid.UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        detail: dict | None = None,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.record(
            event_type,
            result=result,
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            ip_address=ip_address,
            detail=detail,
        )

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

    async def login(self, email: str, password: str, *, ip_address: str | None = None) -> TokenPair:
        user = await self._users.get_by_email(email)
        if user is None or not verify_password(password, user.hashed_password):
            await self._record_audit(
                AuditEventType.LOGIN_FAILURE,
                result=AuditResult.FAILURE,
                actor_user_id=user.id if user else None,
                actor_email=email,
                ip_address=ip_address,
            )
            raise InvalidCredentialsException()
        if not user.is_active:
            await self._record_audit(
                AuditEventType.LOGIN_FAILURE,
                result=AuditResult.FAILURE,
                actor_user_id=user.id,
                actor_email=email,
                ip_address=ip_address,
                detail={"reason": "inactive_user"},
            )
            raise InactiveUserException()

        tokens = await self._issue_token_pair(user)
        await self._record_audit(
            AuditEventType.LOGIN_SUCCESS,
            result=AuditResult.SUCCESS,
            actor_user_id=user.id,
            actor_email=email,
            ip_address=ip_address,
        )
        return tokens

    async def refresh(self, refresh_token: str, *, ip_address: str | None = None) -> TokenPair:
        payload = decode_token(refresh_token, expected_type=TokenType.REFRESH)
        jti = uuid.UUID(payload["jti"])
        user_id = uuid.UUID(payload["sub"])

        stored = await self._refresh_tokens.get_by_jti(jti)

        if stored is not None and stored.revoked and stored.user_id == user_id:
            # Reuse of an already-rotated token: a strong signal of theft
            # (see RefreshTokenRepository.revoke_all_for_user's docstring
            # for the full reasoning). React, don't just reject.
            revoked_count = await self._refresh_tokens.revoke_all_for_user(user_id)
            await self._record_audit(
                AuditEventType.TOKEN_REVOCATION,
                result=AuditResult.FAILURE,
                actor_user_id=user_id,
                ip_address=ip_address,
                detail={"reason": "refresh_token_reuse_detected", "sessions_revoked": revoked_count},
            )
            raise InvalidTokenException(detail="Refresh token has been revoked or is invalid.")

        if stored is None or stored.revoked or stored.user_id != user_id:
            raise InvalidTokenException(detail="Refresh token has been revoked or is invalid.")

        user = await self._users.get_by_id(user_id)
        if user is None or not user.is_active:
            raise InactiveUserException()

        # Rotation: burn the presented refresh token before issuing a new pair.
        await self._refresh_tokens.revoke(stored)
        tokens = await self._issue_token_pair(user)
        await self._record_audit(
            AuditEventType.TOKEN_REFRESH,
            result=AuditResult.SUCCESS,
            actor_user_id=user.id,
            ip_address=ip_address,
        )
        return tokens

    async def logout(self, refresh_token: str, *, ip_address: str | None = None) -> None:
        payload = decode_token(refresh_token, expected_type=TokenType.REFRESH)
        jti = uuid.UUID(payload["jti"])
        stored = await self._refresh_tokens.get_by_jti(jti)
        if stored is not None and not stored.revoked:
            await self._refresh_tokens.revoke(stored)
            await self._record_audit(
                AuditEventType.LOGOUT,
                result=AuditResult.SUCCESS,
                actor_user_id=stored.user_id,
                ip_address=ip_address,
            )
