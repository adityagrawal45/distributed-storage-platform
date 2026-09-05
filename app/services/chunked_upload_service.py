"""
Chunked / resumable upload orchestration (Phase 6).

This is the large-file counterpart to `FileUploadService` (Phase 3): it
owns the multi-request lifecycle of an upload session (initiate -> N
chunk uploads, in any order, from any pod -> complete), rather than a
single request/response upload. Read this docstring before touching
`ChunkedUploadService` — it explains the load-bearing decisions the
whole feature depends on.

GCS architecture (why temp-object-per-chunk + Compose, not one GCS
resumable session):
  A single `Blob.create_resumable_upload_session()` gives resumability
  for free, but is fundamentally a SEQUENTIAL, single-writer stream —
  GCS tracks one persisted-offset cursor per session, and concurrent/
  out-of-order writes to it corrupt the upload (confirmed via GCS
  client-library issue trackers). That satisfies "resumable" but not
  "parallel chunk upload", both of which this phase requires. Instead,
  each chunk is uploaded as its own INDEPENDENT temporary GCS object
  (via the same `StorageService.upload()` Phase 3 already built — a
  chunk is just an ordinary object as far as GCS is concerned), so N
  chunks can genuinely upload concurrently with zero shared state
  between them. At completion, `StorageService.compose_objects()` uses
  GCS's native Compose operation to concatenate the ordered chunk
  objects into the final object — GCS-native, not a custom distributed
  storage mechanism, exactly as required. This is Google's own
  documented pattern for "reassembling files uploaded as multiple
  segments simultaneously."

Why chunk BYTES still pass through FastAPI, given "avoid routing huge
files through the app": the API contract this phase implements is
`PUT /uploads/{id}/chunks/{n}` — the client PUTs chunk bytes to this
service, not directly to a GCS-issued URL. What we DO avoid is buffering
more than ONE chunk (a small, bounded fraction of the total file — see
`Settings.CHUNK_MAX_SIZE_BYTES`) in memory at a time, and we NEVER touch
local disk (see the route handler in `app/api/v1/uploads/routes.py`,
which reads the raw request body directly rather than via Starlette's
`UploadFile`, whose default spooling behavior can fall back to a temp
file on disk for large uploads). The whole multi-GB FILE never exists
in memory or on disk anywhere in this process at once — only ever one
chunk's worth.

Where state actually lives (the distributed-systems contract):
  - PostgreSQL (`UploadSession`, `UploadChunk`) is AUTHORITATIVE for
    upload state — status, which chunks have landed, checksums. Any
    pod, given only an `upload_id`, reconstructs the full picture by
    reading these two tables. This is what makes "Request 1 -> Pod 1,
    Request 2 -> Pod 3, ..." work correctly with zero pod affinity.
  - Google Cloud Storage is AUTHORITATIVE for file *content* — the
    actual chunk bytes and, after completion, the final object.
  - Redis (via `DistributedLockFactory`, reused unchanged from Phase 4)
    is used ONLY for short-lived coordination locks that serialize
    concurrent writers to the same session or the same chunk slot — it
    holds no state that would be lost or wrong if Redis restarted mid-
    upload. If Redis is unavailable, every write operation (chunk
    upload, completion, cancellation) fails closed with a 503 — see
    `_guarded_lock`, which translates infra-layer lock failures into
    `ServiceUnavailableException` — rather than silently skipping
    serialization and risking a lost-update race. Read-only operations
    (status/progress, chunk listing) never touch a lock and keep
    working even with Redis down, since they only read Postgres. See
    `app/core/distributed_lock.py`'s own TTL-based self-healing design
    for why bounded-lock-loss-on-crash is an acceptable trade-off, not
    a new one this phase introduces.

Concurrency model (the specific races this design prevents, and how):
  - Duplicate chunk records: `UploadChunk` has a real DB
    `UniqueConstraint(upload_id, chunk_number)` — the actual guarantee.
    `UploadChunkRepository.create_or_get_existing` attempts the insert
    inside a SAVEPOINT so a losing race doesn't abort the whole request
    transaction. A per-chunk Redis lock additionally makes the race rare
    in the first place, but the DB constraint is what makes it SAFE even
    when the lock doesn't prevent it (e.g. TTL expiry mid-upload).
  - Incorrect uploaded byte count: `UploadSession.uploaded_bytes` is
    NEVER updated via a concurrent read-modify-write increment (a
    textbook lost-update race under parallel chunk uploads). Live
    progress is instead computed by a fresh aggregate query over
    VERIFIED `UploadChunk` rows every time it's requested — slower per
    call, race-free by construction. The column is written exactly
    once, atomically, at completion.
  - Double finalization / concurrent completion: `complete_upload`
    acquires a per-SESSION Redis lock before reading or changing
    `UploadSession.status` at all, so only one completion attempt (on
    any pod) is ever actually running the compose/verify/persist
    sequence at a time. A second, truly concurrent `complete` call
    blocks on the lock, then — once it acquires it — observes
    `status == COMPLETED` and returns the already-created file instead
    of redoing the work (the actual idempotency guarantee for repeated
    completion requests, independent of whether an `Idempotency-Key`
    header was even sent).

Transaction boundaries: every method here does SHORT, focused
read-modify-flush sequences, never holding a Postgres transaction open
across a slow network call to GCS — `flush()` (not `commit`) persists
intermediate state within the request's single transaction (see
`app/database/session.py::get_db`, still the sole commit boundary), and
the genuinely slow operations (chunk upload, compose, checksum
streaming) all happen between flushes, talking to GCS, not holding any
database lock while doing so.

Idempotency: `POST /uploads` and `POST /uploads/{id}/complete` both
accept an optional `Idempotency-Key` header, handled by the SAME
`IdempotencyService` Phase 4 built for `/files/upload` — reused
unchanged, not reimplemented. Chunk uploads (`PUT .../chunks/{n}`) use a
different, more specific mechanism instead (content-checksum comparison
against the already-landed chunk, described above) because the natural
idempotency key for a chunk retry already exists structurally — the
chunk number — so layering the generic `Idempotency-Key` header on top
would be redundant.

Checksum strategy: every chunk is SHA-256'd server-side on arrival and
compared against an optional client-supplied per-chunk checksum before
it's ever written to GCS (fail fast, never pay a network+storage cost
for corrupt data). At completion, GCS's Compose operation does NOT
produce a whole-object hash for composite objects (no MD5, only CRC32C
+ component count) — so a real SHA-256 for the final object is obtained
by `StorageService.compute_object_checksum`, a single bounded streaming
read-through of the composed object, done exactly once per upload. This
is compared against the caller's `expected_checksum` (if supplied at
initiate) and always stored as `actual_checksum` for parity with Phase
3's checksum-driven duplicate detection semantics, even though this
phase doesn't extend content-dedup to the chunked path (see README
Phase 6 "Design Decisions" for that explicit scope boundary).
"""

import hashlib
import io
import math
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.circuit_breaker import CircuitBreaker
from app.core.config import get_settings
from app.core.distributed_lock import DistributedLockFactory
from app.core.enums import ChunkStatus, UploadSessionStatus
from app.core.metrics import CHUNKS_UPLOADED_TOTAL, UPLOAD_BYTES_TOTAL, UPLOAD_RESUMPTIONS_TOTAL, safe_call
from app.core.retry import RetryExhaustedError, retry_async
from app.core.upload_state_machine import UploadStateMachine
from app.events.emitter import OutboxEmitterMixin
from app.events.envelope import EventType
from app.exceptions.custom_exceptions import (
    ChunkChecksumMismatchException,
    ChunkNumberInvalidException,
    ChunkSizeInvalidException,
    DuplicateChunkException,
    DuplicateFileException,
    FileNotFoundException,
    FinalChecksumMismatchException,
    FolderNotFoundException,
    LockAcquisitionException,
    ServiceUnavailableException,
    StorageException,
    UploadAlreadyFinalizedException,
    UploadFailedException,
    UploadIncompleteException,
    UploadSessionExpiredException,
    UploadSessionNotFoundException,
    ValidationException,
)
from app.logging.logger import get_logger
from app.models.file_metadata import FileMetadata, FileStatus, UploadStatus
from app.models.upload_chunk import UploadChunk
from app.models.upload_session import UploadSession
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.repositories.file_version_repository import FileVersionRepository
from app.repositories.folder_repository import FolderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.upload_chunk_repository import UploadChunkRepository
from app.repositories.upload_session_repository import UploadSessionRepository
from app.services.cache_invalidator import CacheInvalidator
from app.services.file_validation_service import FileValidationService
from app.services.storage_service import StorageService

logger = get_logger(__name__)

# One breaker instance per process, shared across requests — protects
# GCS Compose calls specifically (the one operation in this service NOT
# already covered by per-chunk retry_async, since retrying a large
# multi-stage compose is expensive; short-circuiting fast once GCS is
# clearly unhealthy is the better fit there — see module docstring).
_compose_breaker = CircuitBreaker(name="chunked_upload_compose", failure_threshold=5, recovery_timeout=30.0)


@dataclass
class UploadProgress:
    session: UploadSession
    uploaded_chunk_numbers: list[int]
    missing_chunk_numbers: list[int]
    uploaded_bytes: int


class ChunkedUploadService(OutboxEmitterMixin):
    def __init__(
        self,
        upload_session_repository: UploadSessionRepository,
        upload_chunk_repository: UploadChunkRepository,
        file_repository: FileMetadataRepository,
        folder_repository: FolderRepository,
        version_repository: FileVersionRepository,
        storage_service: StorageService,
        validator: FileValidationService,
        lock_factory: DistributedLockFactory,
        invalidator: CacheInvalidator | None = None,
        *,
        outbox: OutboxRepository | None = None,
    ):
        self._sessions = upload_session_repository
        self._chunks = upload_chunk_repository
        self._files = file_repository
        self._folders = folder_repository
        self._versions = version_repository
        self._storage = storage_service
        self._validator = validator
        self._locks = lock_factory
        # Phase 7: optional (keeps every existing direct construction
        # working); completing an upload creates a real FileMetadata row,
        # so the folder's children listing and the owner's search pages
        # must be invalidated exactly as they are for a Phase 3 upload.
        self._invalidator = invalidator
        # Phase 8: keyword-only, None-default (same technique Phase 7 used
        # for `invalidator`) so every pre-existing construction — including
        # all 41 Phase 6 tests — keeps working with no events emitted.
        self._outbox = outbox
        self._settings = get_settings()

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    async def _get_owned_session(self, upload_id: uuid.UUID, owner_id: uuid.UUID) -> UploadSession:
        session = await self._sessions.get_owned(upload_id, owner_id)
        if session is None:
            raise UploadSessionNotFoundException()
        return session

    @staticmethod
    def _is_expired(session: UploadSession) -> bool:
        # SQLite (the test suite's backing store — see tests/conftest.py)
        # round-trips `DateTime(timezone=True)` values as naive
        # datetimes rather than preserving tzinfo, unlike Postgres. This
        # normalizes either case rather than letting the comparison
        # raise `TypeError: can't compare offset-naive and offset-aware
        # datetimes` in tests while working fine against real Postgres.
        expires_at = session.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expires_at

    async def _apply_expiration_if_needed(self, session: UploadSession) -> None:
        """Lazy expiration: checked on every access, no background sweeper this phase — see README Phase 6."""
        if UploadStateMachine.is_terminal(session.status):
            return
        if self._is_expired(session):
            UploadStateMachine.assert_transition(session.status, UploadSessionStatus.EXPIRED)
            session.status = UploadSessionStatus.EXPIRED
            await self._sessions.flush()
            logger.info("upload_expired", upload_id=str(session.id))

    async def _transition_to_uploading_if_needed(self, session: UploadSession) -> None:
        if session.status == UploadSessionStatus.INITIATED:
            UploadStateMachine.assert_transition(session.status, UploadSessionStatus.UPLOADING)
            session.status = UploadSessionStatus.UPLOADING
            await self._sessions.flush()

    @staticmethod
    def _chunk_object_name(final_object_name: str, chunk_number: int) -> str:
        """Temp chunk objects live nested under the final object's own key — easy to identify/clean up by prefix."""
        return f"{final_object_name}.chunks/{chunk_number:06d}"

    @asynccontextmanager
    async def _guarded_lock(self, key: str, ttl_seconds: float):
        """
        Wraps `DistributedLockFactory.lock`, translating an INFRASTRUCTURE
        failure encountered while ACQUIRING the lock (Redis unreachable,
        a connection error, etc.) into `ServiceUnavailableException`
        (503) — a clean domain exception, not a raw client/network
        exception leaking out of the service layer. `LockAcquisitionException`
        (Redis reachable, but the lock is genuinely already held — a
        real, expected 409, not an infra failure) passes through
        unchanged.

        Deliberately guards ONLY the acquire step, not the work done
        inside the `async with` block — a GCS/checksum/validation
        failure that happens while legitimately holding the lock must
        propagate as ITS OWN exception type (`UploadFailedException`,
        `ChunkChecksumMismatchException`, ...), not get reclassified as
        a lock/coordination problem it never was.

        This is the concrete answer to "if Redis becomes unavailable,
        which operations fail": every operation that goes through this
        helper (chunk upload, completion, cancellation — anything that
        mutates `UploadSession`/`UploadChunk` state) fails closed at the
        acquire step rather than silently proceeding without
        coordination and risking a lost-update race. Read-only
        operations (`get_progress`, `list_chunks`) never acquire a lock
        at all and keep working even while Redis is down, since they
        only read Postgres.
        """
        lock = self._locks.lock(key, ttl_seconds=ttl_seconds)
        try:
            await lock.acquire_or_raise()
        except LockAcquisitionException:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberately broad: any Redis/network failure, not a specific type
            logger.error("upload_lock_infrastructure_failure", key=key, error=str(exc))
            raise ServiceUnavailableException(
                "The upload coordination service is temporarily unavailable. Please retry."
            ) from exc

        try:
            yield lock
        finally:
            try:
                await lock.release()
            except Exception as exc:  # noqa: BLE001 - never mask whatever happened inside the lock with a cleanup failure
                logger.warning("upload_lock_release_failed", key=key, error=str(exc))

    # ------------------------------------------------------------------
    # Initiate
    # ------------------------------------------------------------------
    async def initiate_upload(
        self,
        owner_id: uuid.UUID,
        *,
        filename: str,
        total_size: int,
        mime_type: str | None,
        folder_id: uuid.UUID | None,
        chunk_size: int | None,
        expected_checksum: str | None,
        idempotency_key: str | None,
    ) -> UploadSession:
        if folder_id is not None:
            folder = await self._folders.get_active_by_id(folder_id, owner_id)
            if folder is None:
                raise FolderNotFoundException(detail="Target folder not found.")

        validated_filename = self._validator.validate_filename(filename)
        extension = validated_filename.rsplit(".", 1)[-1].lower() if "." in validated_filename else None
        self._validator.validate_extension(extension)

        if total_size <= 0:
            raise ValidationException("Upload size must be greater than zero.")
        if total_size > self._settings.MAX_CHUNKED_UPLOAD_SIZE_BYTES:
            raise ValidationException(
                f"File exceeds the maximum allowed chunked-upload size of "
                f"{self._settings.MAX_CHUNKED_UPLOAD_SIZE_GB} GB."
            )

        resolved_chunk_size = chunk_size or self._settings.CHUNK_DEFAULT_SIZE_BYTES
        if not (self._settings.CHUNK_MIN_SIZE_BYTES <= resolved_chunk_size <= self._settings.CHUNK_MAX_SIZE_BYTES):
            raise ChunkSizeInvalidException(
                f"chunk_size must be between {self._settings.CHUNK_MIN_SIZE_BYTES} and "
                f"{self._settings.CHUNK_MAX_SIZE_BYTES} bytes."
            )

        total_chunks = math.ceil(total_size / resolved_chunk_size)
        if total_chunks > self._settings.MAX_CHUNKS_PER_UPLOAD:
            raise ChunkSizeInvalidException(
                f"This file would require {total_chunks} chunks at the given chunk_size, exceeding the "
                f"maximum of {self._settings.MAX_CHUNKS_PER_UPLOAD}. Use a larger chunk_size."
            )

        if await self._files.name_exists_in_folder(owner_id, folder_id, validated_filename):
            raise DuplicateFileException()

        storage_object = self._storage.generate_object_name(owner_id, extension)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=self._settings.UPLOAD_SESSION_EXPIRATION_MINUTES)

        session = UploadSession(
            owner_id=owner_id,
            folder_id=folder_id,
            filename=validated_filename,
            mime_type=mime_type,
            total_size=total_size,
            chunk_size=resolved_chunk_size,
            total_chunks=total_chunks,
            status=UploadSessionStatus.INITIATED,
            storage_bucket=self._settings.GCS_BUCKET_NAME,
            storage_object=storage_object,
            expected_checksum=expected_checksum,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            created_by=owner_id,
            updated_by=owner_id,
        )
        session = await self._sessions.add(session)

        logger.info(
            "upload_session_created",
            upload_id=str(session.id),
            owner_id=str(owner_id),
            total_size=total_size,
            chunk_size=resolved_chunk_size,
            total_chunks=total_chunks,
        )
        return session

    # ------------------------------------------------------------------
    # Status / progress / chunk listing
    # ------------------------------------------------------------------
    async def get_progress(self, upload_id: uuid.UUID, owner_id: uuid.UUID) -> UploadProgress:
        session = await self._get_owned_session(upload_id, owner_id)
        await self._apply_expiration_if_needed(session)

        verified_chunks = await self._chunks.list_verified_ordered(upload_id)
        uploaded_numbers = [c.chunk_number for c in verified_chunks]
        uploaded_set = set(uploaded_numbers)
        missing_numbers = [n for n in range(1, session.total_chunks + 1) if n not in uploaded_set]
        uploaded_bytes = sum(c.size for c in verified_chunks)

        return UploadProgress(
            session=session,
            uploaded_chunk_numbers=uploaded_numbers,
            missing_chunk_numbers=missing_numbers,
            uploaded_bytes=uploaded_bytes,
        )

    async def list_chunks(self, upload_id: uuid.UUID, owner_id: uuid.UUID) -> list[UploadChunk]:
        await self._get_owned_session(upload_id, owner_id)  # ownership check only
        return await self._chunks.list_for_upload(upload_id)

    # ------------------------------------------------------------------
    # Chunk upload
    # ------------------------------------------------------------------
    async def upload_chunk(
        self,
        upload_id: uuid.UUID,
        owner_id: uuid.UUID,
        chunk_number: int,
        data: bytes,
        *,
        declared_checksum: str | None = None,
    ) -> UploadChunk:
        session = await self._get_owned_session(upload_id, owner_id)
        await self._apply_expiration_if_needed(session)

        if session.status == UploadSessionStatus.EXPIRED:
            raise UploadSessionExpiredException()
        if UploadStateMachine.is_terminal(session.status) or session.status == UploadSessionStatus.COMPLETING:
            raise UploadAlreadyFinalizedException(
                f"Cannot upload chunks to a session in '{session.status.value}' state."
            )

        if not (1 <= chunk_number <= session.total_chunks):
            raise ChunkNumberInvalidException(
                f"chunk_number must be between 1 and {session.total_chunks} for this upload."
            )

        # The last chunk is allowed to be smaller than chunk_size —
        # every other chunk must match it exactly. This is validated
        # BEFORE any network call to GCS, so a client bug never costs a
        # wasted upload.
        expected_size = (
            session.chunk_size
            if chunk_number < session.total_chunks
            else session.total_size - session.chunk_size * (session.total_chunks - 1)
        )
        if len(data) != expected_size:
            raise ChunkSizeInvalidException(f"Chunk {chunk_number} must be exactly {expected_size} bytes; received {len(data)}.")

        checksum = hashlib.sha256(data).hexdigest()
        if declared_checksum and declared_checksum.lower() != checksum:
            raise ChunkChecksumMismatchException(f"Chunk {chunk_number}'s content did not match its declared checksum.")

        try:
            async with self._guarded_lock(
                f"upload-chunk:{upload_id}:{chunk_number}", ttl_seconds=self._settings.IDEMPOTENCY_LOCK_TIMEOUT_SECONDS
            ):
                chunk_row = await self._write_chunk(session, chunk_number, data, checksum)
                await self._transition_to_uploading_if_needed(session)
        except Exception:
            safe_call(lambda: CHUNKS_UPLOADED_TOTAL.labels(result="failure").inc(), operation="chunks_uploaded_inc")
            raise

        safe_call(lambda: CHUNKS_UPLOADED_TOTAL.labels(result="success").inc(), operation="chunks_uploaded_inc")
        logger.info("chunk_upload_completed", upload_id=str(upload_id), chunk_number=chunk_number, size=len(data))
        return chunk_row

    async def _write_chunk(self, session: UploadSession, chunk_number: int, data: bytes, checksum: str) -> UploadChunk:
        existing = await self._chunks.get_by_upload_and_number(session.id, chunk_number)

        if existing is not None and existing.status == ChunkStatus.VERIFIED and existing.checksum == checksum:
            # Safe retry: identical content already landed — genuinely a
            # no-op, no re-upload, no duplicate GCS object. Counted as a
            # "resumption": a client re-sending a chunk it already landed
            # is exactly the resumable-upload-after-a-retry/disconnect
            # signal (see UPLOAD_RESUMPTIONS_TOTAL's docstring).
            logger.info("chunk_upload_replayed", upload_id=str(session.id), chunk_number=chunk_number)
            safe_call(lambda: UPLOAD_RESUMPTIONS_TOTAL.inc(), operation="upload_resumptions_inc")
            return existing

        temp_object_name = self._chunk_object_name(session.storage_object, chunk_number)
        logger.info("chunk_upload_started", upload_id=str(session.id), chunk_number=chunk_number, size=len(data))

        async def _do_upload():
            return await self._storage.upload(
                temp_object_name, io.BytesIO(data), content_type=None, checksum_sha256=checksum, size=len(data)
            )

        try:
            result = await retry_async(
                _do_upload,
                max_attempts=self._settings.RETRY_MAX_ATTEMPTS,
                base_delay=self._settings.RETRY_BASE_DELAY_SECONDS,
                max_delay=self._settings.RETRY_MAX_DELAY_SECONDS,
                retry_on=(StorageException,),
                operation_name="chunk_upload_to_gcs",
            )
        except RetryExhaustedError as exc:
            logger.error(
                "chunk_upload_failed", upload_id=str(session.id), chunk_number=chunk_number, error=str(exc.last_exception)
            )
            raise exc.last_exception from exc

        safe_call(lambda: UPLOAD_BYTES_TOTAL.inc(len(data)), operation="upload_bytes_total_inc")

        if existing is not None:
            # Overwrite: different content for the same chunk number,
            # serialized by the per-chunk lock — last write wins. Clean
            # up the now-stale temp object first.
            if existing.storage_reference and existing.storage_reference != result.object_name:
                await self._storage.delete_many([existing.storage_reference])
            existing.size = len(data)
            existing.checksum = checksum
            existing.status = ChunkStatus.VERIFIED
            existing.storage_reference = result.object_name
            existing.uploaded_at = datetime.now(timezone.utc)
            await self._chunks.flush()
            return existing

        new_chunk = UploadChunk(
            upload_id=session.id,
            chunk_number=chunk_number,
            size=len(data),
            checksum=checksum,
            status=ChunkStatus.VERIFIED,
            storage_reference=result.object_name,
            uploaded_at=datetime.now(timezone.utc),
        )
        chunk_row, created = await self._chunks.create_or_get_existing(new_chunk)
        if not created and chunk_row.checksum != checksum:
            # Lost a race to a concurrent writer for this exact slot
            # despite the lock (e.g. a lock TTL expiry mid-upload) —
            # clean up OUR now-orphaned temp object rather than leaving
            # two objects alive for one chunk_number.
            await self._storage.delete_many([result.object_name])
            raise DuplicateChunkException()
        return chunk_row

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------
    async def complete_upload(self, upload_id: uuid.UUID, owner_id: uuid.UUID, actor_id: uuid.UUID) -> FileMetadata:
        async with self._guarded_lock(f"upload-session:{upload_id}", ttl_seconds=self._settings.LOCK_DEFAULT_TTL_SECONDS):
            session = await self._get_owned_session(upload_id, owner_id)

            if session.status == UploadSessionStatus.COMPLETED:
                # Idempotent: a repeated completion request (with or
                # without an Idempotency-Key) returns the same result
                # instead of redoing (or erroring on) already-finished work.
                if session.file_id is None:  # pragma: no cover - defensive
                    raise UploadIncompleteException("Upload session is marked completed but has no associated file.")
                file = await self._files.get_active_by_id(session.file_id, owner_id)
                if file is None:  # pragma: no cover - defensive
                    raise FileNotFoundException()
                return file

            await self._apply_expiration_if_needed(session)
            if session.status == UploadSessionStatus.EXPIRED:
                raise UploadSessionExpiredException()

            UploadStateMachine.assert_transition(session.status, UploadSessionStatus.COMPLETING)
            session.status = UploadSessionStatus.COMPLETING
            await self._sessions.flush()

            try:
                file = await self._finalize(session, actor_id)
            except Exception:
                session.status = UploadSessionStatus.FAILED
                await self._sessions.flush()
                raise

            return file

    async def _finalize(self, session: UploadSession, actor_id: uuid.UUID) -> FileMetadata:
        verified_chunks = await self._chunks.list_verified_ordered(session.id)
        uploaded_numbers = {c.chunk_number for c in verified_chunks}
        missing = [n for n in range(1, session.total_chunks + 1) if n not in uploaded_numbers]
        if missing:
            preview = missing[:20]
            suffix = f" and {len(missing) - 20} more" if len(missing) > 20 else ""
            raise UploadIncompleteException(f"Cannot complete upload: missing chunk(s) {preview}{suffix}.")

        total_uploaded_size = sum(c.size for c in verified_chunks)
        if total_uploaded_size != session.total_size:
            raise UploadIncompleteException(
                f"Uploaded bytes ({total_uploaded_size}) do not match the declared total_size ({session.total_size})."
            )

        source_object_names = [c.storage_reference for c in verified_chunks]
        logger.info("upload_completing", upload_id=str(session.id), total_chunks=session.total_chunks)

        async def _do_compose():
            return await self._storage.compose_objects(
                session.storage_object, source_object_names, content_type=session.mime_type
            )

        try:
            compose_result = await _compose_breaker.call(_do_compose)
        except UploadFailedException:
            raise
        except Exception as exc:  # noqa: BLE001 - CircuitBreakerOpenException or an unexpected compose failure
            raise UploadFailedException(f"Failed to compose chunks into the final object: {exc}") from exc

        if compose_result.size != session.total_size:
            raise UploadIncompleteException(
                f"Composed object size ({compose_result.size}) does not match declared total_size ({session.total_size})."
            )

        actual_checksum = await self._storage.compute_object_checksum(session.storage_object)

        if session.expected_checksum and session.expected_checksum.lower() != actual_checksum:
            raise FinalChecksumMismatchException(
                "The reassembled file's checksum does not match the checksum declared at upload initiation."
            )

        # MIME re-validation against the REAL bytes — same anti-spoofing
        # guarantee Phase 3 gives single-shot uploads, applied here at
        # the one point a chunked upload actually has the true final
        # bytes available, rather than trusting the client-declared
        # mime_type recorded at initiate.
        sniff_end = min(8191, session.total_size - 1)
        head_bytes = await self._storage.download_range(session.storage_object, 0, sniff_end)
        _, mime_type = self._validator.validate_upload(
            filename=session.filename,
            size=session.total_size,
            head_bytes=head_bytes,
            declared_content_type=session.mime_type,
        )

        if await self._files.name_exists_in_folder(session.owner_id, session.folder_id, session.filename):
            raise DuplicateFileException()

        extension = session.filename.rsplit(".", 1)[-1].lower() if "." in session.filename else None
        stored_filename = f"{uuid.uuid4()}.{extension}" if extension else str(uuid.uuid4())

        file = FileMetadata(
            owner_id=session.owner_id,
            folder_id=session.folder_id,
            original_filename=session.filename,
            stored_filename=stored_filename,
            extension=extension,
            mime_type=mime_type,
            size=compose_result.size,
            checksum=actual_checksum,
            version=1,
            status=FileStatus.ACTIVE,
            storage_provider="gcs",
            bucket_name=compose_result.bucket_name,
            object_name=compose_result.object_name,
            storage_class=compose_result.storage_class,
            etag=compose_result.etag,
            upload_status=UploadStatus.COMPLETED,
            uploaded_at=datetime.now(timezone.utc),
            created_by=actor_id,
            updated_by=actor_id,
        )
        file = await self._files.add(file)
        await self._versions.create(file_id=file.id, version=1, checksum=actual_checksum, size=compose_result.size)

        # Phase 8, event #1 of 2: `file.completed` describes the FILE that
        # now exists — the same shape `file.uploaded` has for Phase 3's
        # single-shot path, so a downstream consumer (thumbnailing,
        # notification) handles both upload paths through one code path.
        await self._emit_event(
            EventType.FILE_COMPLETED,
            aggregate_type="file",
            aggregate_id=file.id,
            user_id=session.owner_id,
            payload={
                "file_id": str(file.id),
                "upload_id": str(session.id),
                "folder_id": str(session.folder_id) if session.folder_id else None,
                "filename": session.filename,
                "content_type": mime_type,
                "size": compose_result.size,
                "checksum": actual_checksum,
                "bucket_name": compose_result.bucket_name,
                "object_name": compose_result.object_name,
            },
        )

        # Best-effort cleanup of the now-redundant per-chunk temp
        # objects — never fails the request; see StorageService.delete_many.
        await self._storage.delete_many(source_object_names)

        session.status = UploadSessionStatus.COMPLETED
        session.actual_checksum = actual_checksum
        session.uploaded_bytes = compose_result.size
        session.file_id = file.id
        session.completed_at = datetime.now(timezone.utc)
        session.updated_by = actor_id
        await self._sessions.flush()

        if self._invalidator is not None:
            await self._invalidator.file_changed(file.id, file.owner_id, file.folder_id)

        # Phase 8, event #2 of 2: `upload.completed` describes the UPLOAD
        # SESSION reaching a terminal state — a different aggregate, a
        # different topic (UPLOAD_EVENTS_TOPIC), and a different audience
        # (anything tracking session lifecycle/quota, rather than anything
        # that cares about the resulting file). Emitted after the session
        # row is flushed COMPLETED so the event and the state agree.
        await self._emit_event(
            EventType.UPLOAD_COMPLETED,
            aggregate_type="upload_session",
            aggregate_id=session.id,
            user_id=session.owner_id,
            payload={
                "upload_id": str(session.id),
                "file_id": str(file.id),
                "filename": session.filename,
                "total_size": compose_result.size,
                "total_chunks": session.total_chunks,
                "checksum": actual_checksum,
            },
        )

        logger.info("upload_completed", upload_id=str(session.id), file_id=str(file.id), size=compose_result.size)
        return file

    # ------------------------------------------------------------------
    # Cancellation / deletion
    # ------------------------------------------------------------------
    async def cancel_upload(self, upload_id: uuid.UUID, owner_id: uuid.UUID) -> UploadSession:
        async with self._guarded_lock(f"upload-session:{upload_id}", ttl_seconds=self._settings.LOCK_DEFAULT_TTL_SECONDS):
            session = await self._get_owned_session(upload_id, owner_id)

            if session.status == UploadSessionStatus.CANCELLED:
                return session  # idempotent no-op

            if session.status == UploadSessionStatus.COMPLETED:
                raise UploadAlreadyFinalizedException()

            await self._apply_expiration_if_needed(session)
            if session.status == UploadSessionStatus.EXPIRED:
                # Already terminal via expiration — cancelling it is a safe no-op.
                return session

            UploadStateMachine.assert_transition(session.status, UploadSessionStatus.CANCELLED)

            chunks = await self._chunks.list_for_upload(upload_id)
            temp_objects = [c.storage_reference for c in chunks if c.storage_reference]
            if temp_objects:
                await self._storage.delete_many(temp_objects)
            await self._chunks.delete_all_for_upload(upload_id)

            session.status = UploadSessionStatus.CANCELLED
            session.cancelled_at = datetime.now(timezone.utc)
            await self._sessions.flush()

        logger.info("upload_cancelled", upload_id=str(upload_id))
        return session

    async def delete_upload(self, upload_id: uuid.UUID, owner_id: uuid.UUID) -> None:
        """
        `DELETE /uploads/{id}` — a strict superset of cancel: cancels
        first if the session is still active (freeing its temp GCS
        objects the same way `cancel_upload` does), then hard-deletes
        the session row itself. A COMPLETED session is never silently
        deleted this way — the resulting file is a real, first-class
        `FileMetadata` row by that point and must go through
        `/files/{id}` deletion instead, which is the deliberately
        distinct trash/permanent-delete flow Phase 2/3 already built.
        """
        session = await self._get_owned_session(upload_id, owner_id)

        if session.status == UploadSessionStatus.COMPLETED:
            raise UploadAlreadyFinalizedException(
                "This upload has already been completed; delete the resulting file via /files instead."
            )

        if not UploadStateMachine.is_terminal(session.status):
            await self.cancel_upload(upload_id, owner_id)
            session = await self._get_owned_session(upload_id, owner_id)

        await self._sessions.delete(session)
        logger.info("upload_session_deleted", upload_id=str(upload_id))
