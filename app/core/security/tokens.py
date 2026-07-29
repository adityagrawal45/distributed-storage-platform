"""
JWT access/refresh token creation and verification.

Design decisions:
- Two distinct token types (`access`, `refresh`) are encoded in the JWT
  `type` claim. This prevents a refresh token from being used directly
  as an access token (and vice versa) even though both are signed with
  the same secret.
- Access tokens are short-lived (default 15 min) and carry the user's
  role so authorization checks don't need a DB round-trip on every
  request.
- Refresh tokens are long-lived, carry minimal claims, and include a
  unique `jti` (JWT ID). This `jti` is what enables refresh-token
  rotation / revocation in Phase 1's design (see `RefreshTokenRepository`)
  — when a refresh token is used, its `jti` is checked against/rotated
  in the database so a stolen, already-used refresh token cannot be
  replayed.
- `iss` (issuer) and `exp` (expiry) are always set; `sub` (subject) is
  the user's UUID as a string, per JWT convention.
"""

import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from jose import JWTError, jwt

from app.core.config import get_settings
from app.exceptions.custom_exceptions import InvalidTokenException, TokenExpiredException

settings = get_settings()


class TokenType(str, Enum):
    ACCESS = "access"
    REFRESH = "refresh"


def _create_token(
    subject: str,
    token_type: TokenType,
    expires_delta: timedelta,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Build and sign a JWT. Returns (token, jti)."""
    now = datetime.now(timezone.utc)
    jti = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type.value,
        "iat": now,
        "exp": now + expires_delta,
        "iss": settings.JWT_ISSUER,
        "jti": jti,
    }
    if extra_claims:
        payload.update(extra_claims)

    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, jti


def create_access_token(user_id: uuid.UUID, role: str) -> str:
    """Create a short-lived access token carrying the user's role."""
    token, _ = _create_token(
        subject=str(user_id),
        token_type=TokenType.ACCESS,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        extra_claims={"role": role},
    )
    return token


def create_refresh_token(user_id: uuid.UUID) -> tuple[str, str, datetime]:
    """
    Create a long-lived refresh token.

    Returns (token, jti, expires_at) so the caller can persist the jti
    and expiry for rotation/revocation tracking.
    """
    expires_delta = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    token, jti = _create_token(
        subject=str(user_id),
        token_type=TokenType.REFRESH,
        expires_delta=expires_delta,
    )
    expires_at = datetime.now(timezone.utc) + expires_delta
    return token, jti, expires_at


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """
    Decode and validate a JWT, enforcing the expected token type.

    Raises TokenExpiredException / InvalidTokenException (domain-level
    exceptions) rather than leaking `jose` internals up to the API layer.
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredException() from exc
    except JWTError as exc:
        raise InvalidTokenException() from exc

    if payload.get("type") != expected_type.value:
        raise InvalidTokenException(detail="Unexpected token type.")

    return payload
