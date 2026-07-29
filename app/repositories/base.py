"""
Generic base repository.

Design decision: the Repository Pattern isolates all persistence/query
concerns from the service (business logic) layer. Services depend on
repository interfaces, never on `AsyncSession`/SQLAlchemy directly. This:
  - Keeps business logic testable with in-memory fakes/mocks.
  - Makes swapping persistence details (e.g. adding read replicas,
    caching, or even a different store) a repository-only change.
  - Follows the Single Responsibility Principle: repositories only know
    how to fetch/persist; they contain no business rules.
"""

import uuid
from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import Base

ModelType = TypeVar("ModelType", bound=Base)


class BaseRepository(Generic[ModelType]):
    """Generic async CRUD repository parametrized over a SQLAlchemy model."""

    model: type[ModelType]

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_by_id(self, entity_id: uuid.UUID) -> ModelType | None:
        result = await self._session.execute(
            select(self.model).where(self.model.id == entity_id)
        )
        return result.scalar_one_or_none()

    async def add(self, entity: ModelType) -> ModelType:
        self._session.add(entity)
        await self._session.flush()
        await self._session.refresh(entity)
        return entity

    async def delete(self, entity: ModelType) -> None:
        await self._session.delete(entity)
        await self._session.flush()
