"""
File upload/download/replace/permanent-delete orchestration (Phase 3).

This is the ONLY service that talks to both `StorageService` (bytes) and
`FileMetadataRepository` (metadata) in the same operation, and therefore
the only place the "two systems, one operation" consistency problem
needs to be solved. See the docstrings on each method for the specific
rollback strategy; the shared principle is:

  Never leave the database pointing at bytes that don't exist, and never
  leave bytes in storage that no database row references, if we can
  possibly help it. Where a rollback step can itself fail (e.g. deleting
  an object after a network partition), we log it loudly and raise
  `RollbackFailedException` rather than silently leaving an orphan —
  an operator paged on that is better than silent, undetected drift
  between Postgres and GCS.

Duplicate-content design decision: when a byte-identical file (same
SHA-256, same owner, not deleted) already exists, this service does NOT
re-upload the bytes — the new metadata row's `object_name`/`bucket_name`
point at the SAME GCS object as the original. This is deliberate
content-addressable-storage behavior: it saves storage cost and upload
bandwidth for the common case of a user uploading the same file into two
different folders, or re-uploading after a client-side retry. The
trade-off this creates — one GCS object can now be referenced by more
than one `FileMetadata` row — is exactly why `permanent_delete` (below)
checks for other referencing rows before deleting the object itself.
"""

import asyncio
import hashlib
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import UploadFile

from app.exceptions.custom_exceptions import (
    DuplicateFileException,
    FileNotFoundException,
    FolderNotFoundException,
    RollbackFailedException,
    ValidationException,
)
from app.events.emitter import OutboxEmitterMixin
from app.events.envelope import EventType
from app.logging.logger import get_logger
from app.models.file_metadata import FileMetadata, FileStatus, UploadStatus
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.repositories.file_version_repository import FileVersionRepository
from app.repositories.folder_repository import FolderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.services.cache_invalidator import CacheInvalidator
from app.services.file_validation_service import FileValidationService
from app.services.storage_service import StorageService

logger = get_logger(__name__)

_HASH_CHUNK_SIZE = 256 * 1024
_SNIFF_BYTES = 8192


class FileUploadService(OutboxEmitterMixin):
    def __init__(
        self,
        file_repository: FileMetadataRepository,
        folder_repository: FolderRepository,
        version_repository: FileVersionRepository,
        storage_service: StorageService,
        validator: FileValidationService,
        *,
        invalidator: CacheInvalidator | None = None,
        outbox: OutboxRepository | None = None,
    ):
        self._files = file_repository
        self._folders = folder_repository
        self._versions = version_repository
        self._storage = storage_service
        self._validator = validator
        # Phase 7: optional so every existing direct construction of this
        # service (including in tests) keeps working unchanged; when
        # present, file writes clear the file/folder-listing/search caches.
        self._invalidator = invalidator
        # Phase 8: same keyword-only/None-default technique, now for the
        # transactional outbox. Bound to the request's session, so the
        # event row commits in the same transaction as the file row.
        self._outbox = outbox

    async def _invalidate_file(self, file: FileMetadata) -> None:
        if self._invalidator is not None:
            await self._invalidator.file_changed(file.id, file.owner_id, file.folder_id)
            await self._invalidator.file_versions_changed(file.id)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    async def _validate_folder(self, owner_id: uuid.UUID, folder_id: uuid.UUID | None) -> None:
        if folder_id is None:
            return
        folder = await self._folders.get_active_by_id(folder_id, owner_id)
        if folder is None:
            raise FolderNotFoundException(detail="Target folder not found.")

    @staticmethod
    async def _hash_and_sniff(upload_file: UploadFile) -> tuple[bytes, str, int]:
        """Streams the upload once to compute its SHA-256, size, and a content-sniffing sample."""
        hasher = hashlib.sha256()
        size = 0
        head_bytes = b""

        await upload_file.seek(0)
        while True:
            chunk = await upload_file.read(_HASH_CHUNK_SIZE)
            if not chunk:
                break
            if not head_bytes:
                head_bytes = chunk[:_SNIFF_BYTES]
            hasher.update(chunk)
            size += len(chunk)
        await upload_file.seek(0)

        return head_bytes, hasher.hexdigest(), size

    @staticmethod
    def _split_extension(filename: str) -> str | None:
        if "." not in filename:
            return None
        return filename.rsplit(".", 1)[-1].lower()

    async def _get_owned_active(self, file_id: uuid.UUID, owner_id: uuid.UUID) -> FileMetadata:
        file = await self._files.get_active_by_id(file_id, owner_id)
        if file is None:
            raise FileNotFoundException()
        return file

    async def _object_still_referenced(self, object_name: str, exclude_id: uuid.UUID | None = None) -> bool:
        """True if some other, non-deleted FileMetadata row still points at this object (dedup safety check)."""
        return await self._files.object_name_in_use(object_name, exclude_id=exclude_id)

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------
    async def upload_file(
        self, owner_id: uuid.UUID, folder_id: uuid.UUID | None, upload_file: UploadFile
    ) -> tuple[FileMetadata, bool]:
        """
        Returns `(file_metadata, is_duplicate)`. Flow: validate folder ->
        validate+hash the upload -> reject duplicate filenames early
        (before spending a network upload) -> dedupe identical content ->
        upload bytes (unless deduped) -> persist metadata + v1 version.

        Rollback: if metadata persistence fails AFTER a real (non-deduped)
        upload succeeded, the just-uploaded object is deleted so we never
        leave an orphaned, unreferenced object in the bucket.
        """
        await self._validate_folder(owner_id, folder_id)

        if not upload_file.filename:
            raise ValidationException("A filename is required.")

        head_bytes, checksum, size = await self._hash_and_sniff(upload_file)
        validated_filename, mime_type = self._validator.validate_upload(
            filename=upload_file.filename,
            size=size,
            head_bytes=head_bytes,
            declared_content_type=upload_file.content_type,
        )
        extension = self._split_extension(validated_filename)

        if await self._files.name_exists_in_folder(owner_id, folder_id, validated_filename):
            raise DuplicateFileException()

        duplicate_source = await self._files.get_by_checksum(owner_id, checksum)
        is_duplicate = duplicate_source is not None

        if is_duplicate:
            logger.info("upload_deduplicated", owner_id=str(owner_id), checksum=checksum)
            object_name = duplicate_source.object_name
            bucket_name = duplicate_source.bucket_name
            etag = duplicate_source.etag
            storage_class = duplicate_source.storage_class
            uploaded_object_this_call = False
        else:
            object_name = self._storage.generate_object_name(owner_id, extension)
            result = await self._storage.upload(
                object_name,
                upload_file.file,
                content_type=mime_type,
                checksum_sha256=checksum,
                size=size,
            )
            bucket_name, etag, storage_class = result.bucket_name, result.etag, result.storage_class
            uploaded_object_this_call = True

        try:
            # `stored_filename` is Phase 2's per-row unique key reservation
            # and must stay unique even when `object_name` (the physical
            # GCS key) is shared across rows via deduplication above.
            stored_filename = f"{uuid.uuid4()}.{extension}" if extension else str(uuid.uuid4())
            file = FileMetadata(
                owner_id=owner_id,
                folder_id=folder_id,
                original_filename=validated_filename,
                stored_filename=stored_filename,
                extension=extension,
                mime_type=mime_type,
                size=size,
                checksum=checksum,
                version=1,
                status=FileStatus.ACTIVE,
                storage_provider="gcs",
                bucket_name=bucket_name,
                object_name=object_name,
                storage_class=storage_class,
                etag=etag,
                upload_status=UploadStatus.COMPLETED,
                uploaded_at=datetime.now(timezone.utc),
                created_by=owner_id,
                updated_by=owner_id,
            )
            file = await self._files.add(file)
            await self._versions.create(file_id=file.id, version=1, checksum=checksum, size=size)
            # Phase 8: emitted INSIDE the same try/transaction as the
            # metadata write, so the event exists if and only if the file
            # row does. If the rollback path below runs, the outbox row is
            # rolled back with everything else — no consumer ever sees an
            # event for a file that was never created.
            await self._emit_event(
                EventType.FILE_UPLOADED,
                aggregate_type="file",
                aggregate_id=file.id,
                user_id=owner_id,
                payload={
                    "file_id": str(file.id),
                    "folder_id": str(folder_id) if folder_id else None,
                    "filename": validated_filename,
                    "content_type": mime_type,
                    "size": size,
                    "checksum": checksum,
                    "bucket_name": bucket_name,
                    "object_name": object_name,
                    "is_duplicate": is_duplicate,
                },
            )
        except Exception:
            if uploaded_object_this_call:
                await self._rollback_object(object_name)
            raise

        await self._invalidate_file(file)
        logger.info("upload_completed", file_id=str(file.id), object_name=object_name, is_duplicate=is_duplicate)
        return file, is_duplicate

    async def _rollback_object(self, object_name: str) -> None:
        logger.warning("rolling_back_orphaned_object", object_name=object_name)
        try:
            await self._storage.delete(object_name)
        except Exception as exc:  # noqa: BLE001
            logger.error("rollback_failed", object_name=object_name, error=str(exc))
            raise RollbackFailedException(
                f"Metadata persistence failed and the rollback delete of '{object_name}' also failed. "
                "This object is now orphaned and requires manual cleanup."
            ) from exc

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------
    async def get_downloadable_file(self, file_id: uuid.UUID, owner_id: uuid.UUID) -> FileMetadata:
        file = await self._get_owned_active(file_id, owner_id)
        if file.upload_status != UploadStatus.COMPLETED or not file.object_name:
            raise FileNotFoundException(detail="This file has no uploaded content yet.")
        return file

    def stream(self, file: FileMetadata) -> AsyncIterator[bytes]:
        return self._to_async_iterator(self._storage.stream_download(file.object_name))

    async def download_range(self, file: FileMetadata, start: int, end: int) -> bytes:
        return await self._storage.download_range(file.object_name, start, end)

    @staticmethod
    async def _to_async_iterator(sync_iterator) -> AsyncIterator[bytes]:
        """
        Bridges the synchronous GCS streaming generator into the async
        world FastAPI's `StreamingResponse` expects, one chunk at a time,
        via `asyncio.to_thread` — so pulling the next chunk over the
        network never blocks the event loop.
        """
        iterator = iter(sync_iterator)
        while True:
            chunk = await asyncio.to_thread(next, iterator, None)
            if chunk is None:
                break
            yield chunk

    # ------------------------------------------------------------------
    # Signed URL
    # ------------------------------------------------------------------
    async def get_signed_url(
        self, file_id: uuid.UUID, owner_id: uuid.UUID, expires_in_minutes: int | None
    ) -> str:
        file = await self.get_downloadable_file(file_id, owner_id)
        return await self._storage.generate_signed_url(file.object_name, expiration_minutes=expires_in_minutes)

    # ------------------------------------------------------------------
    # Replace (new version)
    # ------------------------------------------------------------------
    async def replace_file(
        self, file_id: uuid.UUID, owner_id: uuid.UUID, upload_file: UploadFile, actor_id: uuid.UUID
    ) -> FileMetadata:
        """
        Uploads new bytes to a BRAND NEW object (never overwrites the
        existing one in place), then swaps `FileMetadata` to point at it,
        and only THEN deletes the old object. Doing it in this order
        means a crash or failure at any point still leaves either the old
        object+row (untouched) or the new object+row (fully switched
        over) consistent — never a state where the metadata points at
        bytes that don't exist.
        """
        file = await self._get_owned_active(file_id, owner_id)
        old_object_name = file.object_name

        head_bytes, checksum, size = await self._hash_and_sniff(upload_file)
        _, mime_type = self._validator.validate_upload(
            filename=upload_file.filename or file.original_filename,
            size=size,
            head_bytes=head_bytes,
            declared_content_type=upload_file.content_type,
        )

        new_object_name = self._storage.generate_object_name(owner_id, file.extension)
        result = await self._storage.upload(
            new_object_name, upload_file.file, content_type=mime_type, checksum_sha256=checksum, size=size
        )

        try:
            file.object_name = new_object_name
            file.bucket_name = result.bucket_name
            file.etag = result.etag
            file.storage_class = result.storage_class
            file.mime_type = mime_type
            file.size = size
            file.checksum = checksum
            file.version += 1
            file.upload_status = UploadStatus.COMPLETED
            file.uploaded_at = datetime.now(timezone.utc)
            file.updated_by = actor_id
            await self._versions.create(file_id=file.id, version=file.version, checksum=checksum, size=size)
            await self._emit_event(
                EventType.FILE_VERSION_CREATED,
                aggregate_type="file",
                aggregate_id=file.id,
                user_id=owner_id,
                payload={
                    "file_id": str(file.id),
                    "version": file.version,
                    "content_type": mime_type,
                    "size": size,
                    "checksum": checksum,
                    "bucket_name": result.bucket_name,
                    "object_name": new_object_name,
                    "previous_object_name": old_object_name,
                },
            )
        except Exception:
            await self._rollback_object(new_object_name)
            raise

        if old_object_name and not await self._object_still_referenced(old_object_name, exclude_id=file.id):
            await self._storage.delete(old_object_name)

        await self._invalidate_file(file)
        logger.info("replace_completed", file_id=str(file.id), object_name=new_object_name)
        return file

    # ------------------------------------------------------------------
    # Permanent delete (physical bytes + row)
    # ------------------------------------------------------------------
    async def permanent_delete(self, file_id: uuid.UUID, owner_id: uuid.UUID) -> None:
        """
        Deletes the database row AND, if no other row still references
        the same object (see the dedup design decision above), the GCS
        object itself. Requires the file to already be soft-deleted
        (trashed) first — the same two-step "trash, then permanently
        delete" flow Phase 2 established for metadata-only rows.
        """
        file = await self._files.get_any_by_id(file_id, owner_id)
        if file is None:
            raise FileNotFoundException()
        if not file.is_deleted:
            raise ValidationException("File must be moved to trash before it can be permanently deleted.")

        object_name = file.object_name
        owner = file.owner_id
        folder = file.folder_id
        await self._files.delete(file)

        if self._invalidator is not None:
            await self._invalidator.file_changed(file_id, owner, folder)

        if object_name and not await self._object_still_referenced(object_name):
            await self._storage.delete(object_name)

        logger.info("permanent_delete_completed", file_id=str(file_id))
