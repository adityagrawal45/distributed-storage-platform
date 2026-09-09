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
from sqlalchemy.orm import class_mapper

from app.database.session import Base

ModelType = TypeVar("ModelType", bound=Base)


class BaseRepository(Generic[ModelType]):
    """Generic async CRUD repository parametrized over a SQLAlchemy model."""

    model: type[ModelType]

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_by_id(self, entity_id: uuid.UUID) -> ModelType | None:
        """
        Looks up by primary key, generically — via `class_mapper(...).primary_key`
        rather than `self.model.id` (Phase 12 mypy pass). Every current
        model's primary key happens to be a column literally named `id`,
        but nothing here relies on that convention by name: `Base` (the
        generic bound every model shares) declares no `id` attribute at
        all, so `self.model.id` was only ever valid because every
        CONCRETE subclass instantiated here happens to declare one —
        invisible to mypy, and not actually guaranteed by the type
        `ModelType` is bound to. Asking SQLAlchemy's own mapper for the
        primary key column is both correctly typed and the more robust
        statement of what this method actually means: "find the row
        whose primary key is `entity_id`," regardless of that column's
        name.
        """
        pk_column = class_mapper(self.model).primary_key[0]
        result = await self._session.execute(select(self.model).where(pk_column == entity_id))
        return result.scalar_one_or_none()

    async def add(self, entity: ModelType) -> ModelType:
        self._session.add(entity)
        await self._session.flush()
        await self._session.refresh(entity)
        return entity

    async def delete(self, entity: ModelType) -> None:
        await self._session.delete(entity)
        await self._session.flush()

    async def flush(self) -> None:
        """
        Persists in-place mutations of an already-tracked entity (one
        previously returned by `get_by_id`/`add`) without waiting for
        the request-boundary commit in `app.database.session.get_db`.

        Added for Phase 6: `ChunkedUploadService` mutates
        `UploadSession.status` repeatedly within a single request (e.g.
        UPLOADING -> COMPLETING -> COMPLETED) and needs later reads in
        that same request/transaction to see the latest value —
        `autoflush=False` on the session factory means that otherwise
        wouldn't happen automatically. Existing services haven't needed
        this because they either mutate-then-return (Phase 3's
        `replace_file`) or only ever create new rows (`add`, which
        already flushes internally).
        """
        await self._session.flush()
