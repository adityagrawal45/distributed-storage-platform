"""
Google Cloud Storage integration service.

Responsibilities: upload, download, delete, replace, signed URLs, object
naming, and translating GCS/`google-api-core` errors into our domain
exceptions. This is the ONLY module in the codebase that imports
`google.cloud.storage` — nothing else should ever touch a `Blob` or
`Bucket` directly, which is what makes this swappable for a different
backend (e.g. S3) in a future phase without touching any service that
depends on it.

Design decisions:
- Object naming (`generate_object_name`): never the user's original
  filename. A path of `{tenant}/{owner_id}/{year}/{month}/{uuid4}{ext}`
  is used instead, because:
    1. User-controlled filenames can contain characters that are legal
       in a filesystem/UI but unsafe or ambiguous as a GCS object key
       (path traversal segments, unicode normalization tricks, extremely
       long names) — generating our own name removes that entire attack
       surface.
    2. Two different files (possibly by different users) can share an
       `original_filename` (e.g. "invoice.pdf"); GCS object names must
       be globally unique within a bucket, so deriving the key from a
       UUID guarantees no collisions regardless of what users name things.
    3. The `{owner}/{year}/{month}/...` prefix keeps a natural sharding
       key for GCS's internal load balancing (which partitions by key
       prefix) and gives cheap "list a user's objects from a given
       month" operations for future lifecycle/archival tooling, without
       needing a database round-trip.
    4. Renaming a file (`original_filename`) never requires renaming the
       underlying object — the object name is permanent for the object's
       lifetime, exactly mirroring the `stored_filename` reservation
       Phase 2 already established.
- All GCS SDK calls run inside `asyncio.to_thread`: `google-cloud-storage`
  is a synchronous SDK; running it directly on the event loop would block
  every other concurrent request for the duration of the network call.
- Buckets are always private (uniform bucket-level access, no `public_url`
  ever generated as a real public link) — see README "Security" section.
  Temporary access is granted exclusively via time-boxed V4 signed URLs.
- SHA-256 (not GCS's own MD5/CRC32C) is the integrity checksum stored on
  `FileMetadata.checksum`, because it's what's used for duplicate-content
  detection across the whole platform (schemas/services agree on one
  algorithm), and because MD5 is not appropriate to rely on for content
  addressing in new code. It's stored as GCS custom object metadata too,
  so `verify_upload` can confirm round-trip integrity independently of
  whatever transport-level hash GCS itself tracks.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import BinaryIO, Iterator

from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage

from app.core.config import get_settings
from app.exceptions.custom_exceptions import (
    BucketNotFoundException,
    StorageException,
    StorageObjectNotFoundException,
    StoragePermissionException,
    StorageTimeoutException,
    UploadFailedException,
)
from app.logging.logger import get_logger

logger = get_logger(__name__)

DEFAULT_CHUNK_SIZE = 256 * 1024  # 256 KiB streaming chunk size


@dataclass(frozen=True)
class UploadResult:
    object_name: str
    bucket_name: str
    etag: str | None
    size: int
    storage_class: str | None
    checksum: str


def _translate_gcs_error(exc: Exception, *, context: str) -> StorageException:
    """Maps a raw google-api-core exception to a domain-level StorageException."""
    if isinstance(exc, gcs_exceptions.NotFound):
        return StorageObjectNotFoundException(f"{context}: object or bucket not found.")
    if isinstance(exc, (gcs_exceptions.Forbidden, gcs_exceptions.Unauthorized)):
        return StoragePermissionException(f"{context}: permission denied by storage backend.")
    if isinstance(exc, gcs_exceptions.DeadlineExceeded):
        return StorageTimeoutException(f"{context}: storage backend timed out.")
    if isinstance(exc, gcs_exceptions.GoogleAPIError):
        return StorageException(f"{context}: storage backend error ({exc.__class__.__name__}).")
    return StorageException(f"{context}: unexpected storage error.")


class StorageService:
    """Thin, testable wrapper around the GCS SDK. No business logic — see FileUploadService for that."""

    def __init__(self, client: storage.Client, bucket_name: str | None = None):
        self._client = client
        settings = get_settings()
        self._bucket_name = bucket_name or settings.GCS_BUCKET_NAME
        self._default_expiration_minutes = settings.SIGNED_URL_EXPIRATION_MINUTES

    # ------------------------------------------------------------------
    # Object naming
    # ------------------------------------------------------------------
    @staticmethod
    def generate_object_name(owner_id: uuid.UUID, extension: str | None, tenant_id: str = "default") -> str:
        """Builds a unique, non-user-controlled object key. See module docstring for rationale."""
        now = datetime.now(timezone.utc)
        suffix = f"{uuid.uuid4()}.{extension.lstrip('.').lower()}" if extension else str(uuid.uuid4())
        return f"{tenant_id}/{owner_id}/{now.year:04d}/{now.month:02d}/{suffix}"

    def _bucket(self) -> storage.Bucket:
        return self._client.bucket(self._bucket_name)

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------
    async def upload(
        self,
        object_name: str,
        stream: BinaryIO,
        *,
        content_type: str | None,
        checksum_sha256: str,
        size: int,
    ) -> UploadResult:
        """
        Streams `stream`'s current contents to `object_name`. Caller is
        responsible for having the stream positioned at offset 0.
        """
        logger.info("storage_upload_started", object_name=object_name, size=size)

        def _do_upload() -> storage.Blob:
            blob = self._bucket().blob(object_name)
            blob.metadata = {"sha256": checksum_sha256}
            blob.upload_from_file(stream, content_type=content_type, size=size)
            blob.reload()
            return blob

        try:
            blob = await asyncio.to_thread(_do_upload)
        except gcs_exceptions.NotFound as exc:
            logger.error("storage_upload_failed", object_name=object_name, error=str(exc))
            raise BucketNotFoundException("Upload failed: configured bucket does not exist.") from exc
        except Exception as exc:  # noqa: BLE001 - translated below, nothing swallowed
            logger.error("storage_upload_failed", object_name=object_name, error=str(exc))
            translated = _translate_gcs_error(exc, context="Upload")
            raise UploadFailedException(translated.detail) from exc

        logger.info("storage_upload_completed", object_name=object_name, etag=blob.etag, size=blob.size)
        return UploadResult(
            object_name=object_name,
            bucket_name=self._bucket_name,
            etag=blob.etag,
            size=blob.size or size,
            storage_class=blob.storage_class,
            checksum=checksum_sha256,
        )

    async def replace(
        self,
        object_name: str,
        stream: BinaryIO,
        *,
        content_type: str | None,
        checksum_sha256: str,
        size: int,
    ) -> UploadResult:
        """Overwrites an existing object's bytes in place. Same semantics as `upload`."""
        return await self.upload(
            object_name, stream, content_type=content_type, checksum_sha256=checksum_sha256, size=size
        )

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------
    def stream_download(self, object_name: str, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[bytes]:
        """
        Synchronous generator yielding an object's bytes in chunks.
        Intended to be driven from a worker thread (see the `/files/{id}/download`
        route, which wraps iteration via `asyncio.to_thread`-friendly `StreamingResponse`
        semantics) so large downloads never buffer the whole file in memory.
        """
        logger.info("storage_download_started", object_name=object_name)
        blob = self._bucket().blob(object_name)
        try:
            with blob.open("rb", chunk_size=chunk_size) as handle:
                while True:
                    chunk = handle.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        except gcs_exceptions.NotFound as exc:
            raise StorageObjectNotFoundException("The file's bytes could not be located in storage.") from exc
        except Exception as exc:  # noqa: BLE001
            raise _translate_gcs_error(exc, context="Download") from exc
        logger.info("storage_download_completed", object_name=object_name)

    async def download_range(self, object_name: str, start: int, end: int) -> bytes:
        """
        Fetches a bounded byte range `[start, end]` (inclusive) in one
        call — backs HTTP `Range` request support for the download
        endpoint (e.g. resumable/seekable downloads, media scrubbing).
        """

        def _do_download() -> bytes:
            blob = self._bucket().blob(object_name)
            return blob.download_as_bytes(start=start, end=end)

        try:
            return await asyncio.to_thread(_do_download)
        except gcs_exceptions.NotFound as exc:
            raise StorageObjectNotFoundException("The file's bytes could not be located in storage.") from exc
        except Exception as exc:  # noqa: BLE001
            raise _translate_gcs_error(exc, context="Range download") from exc

    async def get_blob_metadata(self, object_name: str) -> storage.Blob:
        """Fetches an object's metadata (size, content_type, etag) without downloading its bytes."""

        def _reload() -> storage.Blob:
            blob = self._bucket().blob(object_name)
            blob.reload()
            return blob

        try:
            return await asyncio.to_thread(_reload)
        except Exception as exc:  # noqa: BLE001
            raise _translate_gcs_error(exc, context="Metadata lookup") from exc

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------
    async def delete(self, object_name: str) -> None:
        logger.info("storage_delete_started", object_name=object_name)

        def _do_delete() -> None:
            self._bucket().blob(object_name).delete()

        try:
            await asyncio.to_thread(_do_delete)
        except gcs_exceptions.NotFound:
            # Already gone — deleting a nonexistent object is not an error
            # for our purposes (idempotent delete), it just gets logged.
            logger.warning("storage_delete_object_already_absent", object_name=object_name)
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("storage_delete_failed", object_name=object_name, error=str(exc))
            raise _translate_gcs_error(exc, context="Delete") from exc

        logger.info("storage_delete_completed", object_name=object_name)

    # ------------------------------------------------------------------
    # Signed URLs
    # ------------------------------------------------------------------
    async def generate_signed_url(
        self, object_name: str, *, expiration_minutes: int | None = None, method: str = "GET"
    ) -> str:
        """
        Issues a V4 signed URL, time-boxed to `expiration_minutes`
        (defaults to `settings.SIGNED_URL_EXPIRATION_MINUTES`). The
        bucket itself is never made public — see README Security section
        — this is the only sanctioned way a client obtains direct access
        to an object's bytes.
        """
        minutes = expiration_minutes or self._default_expiration_minutes
        logger.info("signed_url_generation_started", object_name=object_name, expiration_minutes=minutes)

        def _do_generate() -> str:
            blob = self._bucket().blob(object_name)
            return blob.generate_signed_url(version="v4", expiration=timedelta(minutes=minutes), method=method)

        try:
            url = await asyncio.to_thread(_do_generate)
        except Exception as exc:  # noqa: BLE001
            logger.error("signed_url_generation_failed", object_name=object_name, error=str(exc))
            raise _translate_gcs_error(exc, context="Signed URL generation") from exc

        logger.info("signed_url_generation_completed", object_name=object_name)
        return url

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------
    async def verify_upload(self, object_name: str, *, expected_size: int, expected_checksum: str) -> bool:
        """Confirms an object exists in storage and its size/checksum match what we recorded."""
        try:
            blob = await self.get_blob_metadata(object_name)
        except StorageObjectNotFoundException:
            return False

        stored_checksum = (blob.metadata or {}).get("sha256")
        return blob.size == expected_size and stored_checksum == expected_checksum
