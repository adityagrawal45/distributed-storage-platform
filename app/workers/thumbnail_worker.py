"""
Thumbnail worker.

Run with: `python -m app.workers.thumbnail_worker`

Consumes `thumbnail.requested` off FILE_EVENTS_TOPIC (published by the
file-processing worker), generates a thumbnail via `ThumbnailService`,
and records the result on `FileMetadata.thumbnail_object_name`.

Why this is a separate worker from file processing
--------------------------------------------------
Resource profile. Decoding an image is the only genuinely CPU- and
memory-hungry thing in this system — a 50-megapixel PNG expands to
hundreds of megabytes decoded. Running it in the same process as file
validation would mean sizing the file worker's memory limit for the
worst-case image, and one bad decode OOM-killing a pod that was also
responsible for fan-out. Separate deployments mean the thumbnail worker
gets its own (higher) memory limit and its own replica count, and its
failures are contained.

The DB write is the LAST step, on purpose
-----------------------------------------
Generate and upload the thumbnail first, then point the metadata row at
it. In the reverse order, a crash between the two would leave
`thumbnail_object_name` referencing an object that does not exist — a
dangling pointer served to users. In this order the worst outcome is an
orphaned thumbnail object nothing references, which costs a few kilobytes
and is trivially reclaimable, and which the next redelivery overwrites in
place anyway because the thumbnail object name is deterministic.

A missing `FileMetadata` row is non-retryable: the file was permanently
deleted between upload and thumbnailing, which is a legitimate race with
a permanent answer — there is nothing to attach a thumbnail to, ever.
"""

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.gcs import get_storage_client
from app.events.envelope import EventEnvelope, EventType
from app.exceptions.custom_exceptions import NonRetryableEventError
from app.logging.logger import get_logger
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.services.storage_service import StorageService
from app.services.thumbnail_service import ThumbnailService
from app.workers.base import BaseWorker

logger = get_logger(__name__)

WORKER_NAME = "thumbnail-worker"


class ThumbnailWorker(BaseWorker):
    worker_name = WORKER_NAME
    consumer_name = WORKER_NAME

    def __init__(
        self,
        *,
        thumbnail_service: ThumbnailService | None = None,
        storage_service: StorageService | None = None,
        **kwargs,
    ):
        self._storage = storage_service
        self._thumbnails = thumbnail_service
        super().__init__(**kwargs)

    def default_subscription(self) -> str:
        return get_settings().THUMBNAIL_WORKER_SUBSCRIPTION

    def interested_in(self, envelope: EventEnvelope) -> bool:
        return envelope.event_type == EventType.THUMBNAIL_REQUESTED

    def _resolve_thumbnails(self) -> ThumbnailService:
        if self._thumbnails is None:
            storage = self._storage or StorageService(get_storage_client())
            self._thumbnails = ThumbnailService(storage)
        return self._thumbnails

    async def process(self, envelope: EventEnvelope, session: AsyncSession) -> None:
        payload = envelope.payload
        raw_file_id = payload.get("file_id")
        object_name = payload.get("object_name")
        content_type = payload.get("content_type")

        if not raw_file_id or not object_name:
            raise NonRetryableEventError(
                f"thumbnail.requested {envelope.event_id} is missing file_id/object_name."
            )

        try:
            file_id = uuid.UUID(str(raw_file_id))
        except ValueError as exc:
            raise NonRetryableEventError(f"Malformed file_id {raw_file_id!r} in thumbnail request.") from exc

        # The allow-list check happens FIRST, before any download and
        # certainly before any decode — see ThumbnailService's docstring
        # on why that ordering is a security property, not an
        # optimization.
        thumbnails = self._resolve_thumbnails()
        thumbnails.assert_supported(content_type)

        files = FileMetadataRepository(session)
        file = await files.get_by_id(file_id)
        if file is None:
            # Permanently deleted between upload and thumbnailing. A real
            # race, with a permanent answer: there is nothing to attach a
            # thumbnail to and there never will be again.
            raise NonRetryableEventError(f"File {file_id} no longer exists; nothing to thumbnail.")

        result = await thumbnails.generate(
            file_id=file_id, object_name=object_name, content_type=content_type
        )

        # LAST step — see the module docstring on ordering.
        file.thumbnail_object_name = result.object_name
        await files.flush()

        logger.info(
            "thumbnail_attached",
            file_id=str(file_id),
            thumbnail_object=result.object_name,
            width=result.width,
            height=result.height,
        )


def main() -> None:  # pragma: no cover - process entrypoint
    from app.logging.logger import configure_logging

    configure_logging()
    asyncio.run(ThumbnailWorker().run())


if __name__ == "__main__":  # pragma: no cover
    main()
