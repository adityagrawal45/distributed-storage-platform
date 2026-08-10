"""
UploadSession persistence (Phase 6).

Design decision: `get_owned` is the ONLY read path routes/services use
to fetch a session by ID — it scopes to `owner_id` in the same query
(rather than fetching by ID and checking ownership in Python
afterwards), so an upload session belonging to another user is
indistinguishable from one that doesn't exist at all (`None` either
way), matching the existing `FileMetadataRepository.get_active_by_id`
convention and avoiding leaking existence information via a different
error shape.
"""

import uuid

from sqlalchemy import select

from app.models.upload_session import UploadSession
from app.repositories.base import BaseRepository


class UploadSessionRepository(BaseRepository[UploadSession]):
    model = UploadSession

    async def get_owned(self, upload_id: uuid.UUID, owner_id: uuid.UUID) -> UploadSession | None:
        result = await self._session.execute(
            select(UploadSession).where(UploadSession.id == upload_id, UploadSession.owner_id == owner_id)
        )
        return result.scalar_one_or_none()
