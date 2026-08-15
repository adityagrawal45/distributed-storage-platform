"""
User service — business logic for user retrieval/management.

Phase 7 (caching) note on authorization, stated per-entity as the spec
requires:

`GET /users/{id}` is ADMIN-only, and that authorization decision is made
by the `require_role(UserRole.ADMIN)` dependency BEFORE this service is
ever called — it is a property of the *caller*, not of the cached user
row. So the cached value (a `UserRead`, which by construction never
contains `hashed_password`) is identical for every caller allowed to see
it, and the key is correctly resource-scoped (`nimbusfs:user:{id}`) rather
than caller-scoped. Caching a *response including an authorization
outcome* would be the dangerous pattern; caching the resource behind an
unchanged authorization gate is not.

What is deliberately NOT cached: `get_current_user`
(`app/dependencies/auth.py`) still reads the user row from Postgres on
every authenticated request. Phase 1 made that call explicitly so that
deactivating an account takes effect immediately rather than at token
expiry; serving it from a 15-minute cache would silently undo that
security property. The read cache here applies to the *profile endpoint*
only.
"""

import uuid

from app.core.cache.policy import CacheEntity
from app.exceptions.custom_exceptions import NotFoundException
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user import UserRead
from app.services.cache_invalidator import CacheInvalidator
from app.services.cache_service import CacheService


class UserService:
    def __init__(
        self,
        user_repository: UserRepository,
        *,
        cache: CacheService | None = None,
        invalidator: CacheInvalidator | None = None,
    ):
        self._users = user_repository
        self._cache = cache
        self._invalidator = invalidator

    async def get_by_id(self, user_id: uuid.UUID) -> User:
        """Uncached ORM read — kept unchanged for callers that need the entity itself."""
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise NotFoundException(detail="User not found.")
        return user

    async def get_profile(self, user_id: uuid.UUID) -> UserRead:
        """
        Cache-aside read of a user's public profile.

        Returns the API schema, not the ORM entity: a cached value has to
        be JSON, and reconstituting a detached SQLAlchemy object from JSON
        would be a lie (it would not be session-attached, and any lazy
        attribute access on it would fail confusingly). Returning the
        schema the route was going to build anyway keeps the cached and
        uncached paths structurally identical.
        """
        if self._cache is None or not self._cache.enabled:
            return UserRead.model_validate(await self.get_by_id(user_id))

        key = self._cache.keys.user(user_id)

        async def _load() -> dict:
            user = await self.get_by_id(user_id)
            return UserRead.model_validate(user).model_dump(mode="json")

        payload = await self._cache.get_or_set(
            key, _load, self._cache.ttl_for(CacheEntity.USER), entity=CacheEntity.USER
        )
        return UserRead.model_validate(payload)

    async def invalidate_user(self, user_id: uuid.UUID) -> None:
        """Hook for any future write path that mutates a user row."""
        if self._invalidator is not None:
            await self._invalidator.user_changed(user_id)
