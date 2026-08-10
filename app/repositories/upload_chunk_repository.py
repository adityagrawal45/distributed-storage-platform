"""
UploadChunk persistence (Phase 6).

Design decisions:
- `create_or_get_existing` is the one write path chunk uploads go
  through. It attempts the INSERT inside a SAVEPOINT
  (`session.begin_nested()`), and if the `(upload_id, chunk_number)`
  unique constraint rejects it (a genuine concurrent-write race that
  slipped past the per-chunk Redis lock — see ChunkedUploadService),
  only that SAVEPOINT rolls back, not the whole request transaction,
  and the caller gets back whatever row actually won the race instead
  of a request-ending `IntegrityError`. This is the real, database-
  level guarantee behind "prevent duplicate chunk records" — the Redis
  lock makes the race rare, this makes it safe even when it happens.
- Progress is computed with live aggregate queries
  (`sum_verified_bytes`, `count_verified`), never a maintained counter
  — see `UploadSession.uploaded_bytes`'s docstring for why a
  concurrently-incremented counter is the wrong tool here.
- `delete_all_for_upload` issues a single bulk `DELETE`, not a
  fetch-then-delete-each-row loop — used by cancellation, where
  potentially thousands of chunk rows (bounded by
  `Settings.MAX_CHUNKS_PER_UPLOAD`) need to disappear in one short
  transaction rather than N round trips.
"""

import uuid

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.enums import ChunkStatus
from app.models.upload_chunk import UploadChunk
from app.repositories.base import BaseRepository


class UploadChunkRepository(BaseRepository[UploadChunk]):
    model = UploadChunk

    async def get_by_upload_and_number(self, upload_id: uuid.UUID, chunk_number: int) -> UploadChunk | None:
        result = await self._session.execute(
            select(UploadChunk).where(UploadChunk.upload_id == upload_id, UploadChunk.chunk_number == chunk_number)
        )
        return result.scalar_one_or_none()

    async def list_for_upload(self, upload_id: uuid.UUID) -> list[UploadChunk]:
        result = await self._session.execute(
            select(UploadChunk).where(UploadChunk.upload_id == upload_id).order_by(UploadChunk.chunk_number)
        )
        return list(result.scalars().all())

    async def list_verified_ordered(self, upload_id: uuid.UUID) -> list[UploadChunk]:
        """Only VERIFIED chunks, ordered by chunk_number — the exact sequence Compose needs."""
        result = await self._session.execute(
            select(UploadChunk)
            .where(UploadChunk.upload_id == upload_id, UploadChunk.status == ChunkStatus.VERIFIED)
            .order_by(UploadChunk.chunk_number)
        )
        return list(result.scalars().all())

    async def sum_verified_bytes(self, upload_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.coalesce(func.sum(UploadChunk.size), 0)).where(
                UploadChunk.upload_id == upload_id, UploadChunk.status == ChunkStatus.VERIFIED
            )
        )
        return int(result.scalar_one())

    async def count_verified(self, upload_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.count()).where(UploadChunk.upload_id == upload_id, UploadChunk.status == ChunkStatus.VERIFIED)
        )
        return int(result.scalar_one())

    async def create_or_get_existing(self, chunk: UploadChunk) -> tuple[UploadChunk, bool]:
        """
        Attempts to insert `chunk`. Returns `(row, created)` — `created`
        is `False` if a concurrent writer already claimed this
        `(upload_id, chunk_number)` slot first, in which case `row` is
        THEIR row, not `chunk`. See module docstring for the SAVEPOINT
        rationale.
        """
        try:
            async with self._session.begin_nested():
                self._session.add(chunk)
                await self._session.flush()
            await self._session.refresh(chunk)
            return chunk, True
        except IntegrityError:
            existing = await self.get_by_upload_and_number(chunk.upload_id, chunk.chunk_number)
            if existing is None:  # pragma: no cover - defensive; the constraint violation implies a row exists
                raise
            return existing, False

    async def delete_all_for_upload(self, upload_id: uuid.UUID) -> None:
        await self._session.execute(sa_delete(UploadChunk).where(UploadChunk.upload_id == upload_id))
        await self._session.flush()
