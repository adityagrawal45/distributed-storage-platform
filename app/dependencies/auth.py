"""
Authentication & authorization dependencies.

Design decisions:
- `OAuth2PasswordBearer` tells Swagger UI where to obtain a token
  (`tokenUrl`) and extracts the `Authorization: Bearer <token>` header
  for us — no manual header parsing.
- `get_current_user` decodes + validates the access token AND re-checks
  `is_active` against the database on every request. This is a
  deliberate trade-off: it costs one extra DB lookup per request, but
  it means deactivating a user takes effect immediately, rather than
  waiting up to `ACCESS_TOKEN_EXPIRE_MINUTES` for their token to expire.
- `require_role(...)` is a dependency *factory*: it returns a dependency
  callable closed over the allowed roles, so routes declare
  authorization requirements at the signature level
  (`Depends(require_role(UserRole.ADMIN))`) rather than with imperative
  `if` checks buried in the handler body.
"""

import uuid
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer

from app.core.config import get_settings
from app.core.security.tokens import TokenType, decode_token
from app.dependencies.providers import UserRepositoryDep
from app.exceptions.custom_exceptions import AuthorizationException, InactiveUserException, InvalidTokenException
from app.models.user import User, UserRole

settings = get_settings()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_PREFIX}/auth/login")


async def get_current_user(
    request: Request,
    token: Annotated[str, Depends(oauth2_scheme)],
    user_repository: UserRepositoryDep,
) -> User:
    """Resolve the authenticated user from a validated access token."""
    payload = decode_token(token, expected_type=TokenType.ACCESS)

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise InvalidTokenException() from exc

    user = await user_repository.get_by_id(user_id)
    if user is None:
        raise InvalidTokenException(detail="User no longer exists.")
    if not user.is_active:
        raise InactiveUserException()

    # Stashed on request.state so RequestContextMiddleware's completion
    # log line can include *who* made the request without threading a
    # user ID through every route signature — see
    # app/middleware/request_context.py.
    request.state.user_id = user.id

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*allowed_roles: UserRole):
    """Dependency factory enforcing role-based access control on a route."""

    async def _dependency(current_user: CurrentUser) -> User:
        if current_user.role not in allowed_roles:
            raise AuthorizationException(
                detail=f"This action requires one of the following roles: "
                f"{', '.join(r.value for r in allowed_roles)}."
            )
        return current_user

    return _dependency
