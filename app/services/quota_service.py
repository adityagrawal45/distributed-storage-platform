"""
`QuotaService` — the one place NimbusFS enforces per-organization
storage quotas (Phase 13 §21-23).

See `OrganizationRepository.try_reserve_storage`'s docstring for the
actual concurrency mechanism (a single guarded `UPDATE`, not a
SELECT-then-UPDATE) — this service is a thin, named wrapper over it so
call sites read as intent ("reserve quota for this upload") rather
than a raw repository call, and so the metric/exception/audit side
effects of a rejection live in exactly one place.

What counts against quota, and when (Phase 13 §22)
----------------------------------------------------
- **Reserved at upload COMPLETION, not at upload START.** A quota
  check at start-of-upload would have to guess the final size (for
  chunked uploads, the client-declared `total_size` — trustworthy
  enough to validate against, per `ChunkedUploadService`'s existing
  size checks, but reserving quota against a number that isn't yet
  real bytes on disk would let an abandoned/failed upload hold quota
  hostage indefinitely with no natural release trigger). Reserving at
  completion means the reservation and the bytes landing in GCS happen
  in the same operation, bounded by the SAME transaction that persists
  the `FileMetadata`/`UploadSession` row.
- **Released on deletion, restore-then-permanent-delete, and a de-duplicated
  upload's non-consumption.** A content-addressed duplicate (Phase 3's
  existing dedup: identical checksum -> same GCS object, new metadata
  row) reserves `file_count=+1` but `storage_used_bytes=+0` — the bytes
  already exist and are not re-counted, which is also why quota
  tracks `storage_used_bytes` as "unique bytes this org is charged
  for," not "sum of every FileMetadata.size row" (a dedup'd file's
  `.size` still reports the real file size for display purposes; the
  quota reservation for that specific upload call is zero).
- **A cancelled/failed/expired chunked upload never reserved anything**
  (reservation happens at completion, per above) — so there is nothing
  to release for those paths, by construction, not because a release
  call was forgotten.
"""

from __future__ import annotations

import uuid

from app.core.metrics import QUOTA_EXCEEDED_TOTAL, safe_call
from app.logging.logger import get_logger
from app.repositories.organization_repository import OrganizationRepository

logger = get_logger(__name__)


class QuotaService:
    def __init__(self, organization_repository: OrganizationRepository):
        self._organizations = organization_repository

    async def try_reserve(self, organization_id: uuid.UUID, *, size_bytes: int, file_delta: int = 1) -> bool:
        """
        Attempts to reserve `size_bytes` (+`file_delta` toward
        `file_count`) against the organization's quota. Returns True on
        success (the reservation is already committed-in-this-
        transaction at that point — see the repository method's
        docstring); False means the caller MUST NOT proceed with
        whatever operation this reservation was for.
        """
        ok = await self._organizations.try_reserve_storage(organization_id, size_bytes, file_delta)
        if not ok:
            safe_call(lambda: QUOTA_EXCEEDED_TOTAL.inc(), operation="quota_exceeded_total_inc")
            logger.warning(
                "quota_exceeded",
                organization_id=str(organization_id),
                requested_bytes=size_bytes,
                requested_files=file_delta,
            )
        return ok

    async def release(self, organization_id: uuid.UUID, *, size_bytes: int, file_delta: int = 1) -> None:
        """Releases quota (deletion/cancellation) — `size_bytes`/`file_delta` are the POSITIVE amounts
        being given back; this method negates them internally so callers never have to remember which
        sign means "give back" at the call site."""
        await self._organizations.release_storage(organization_id, -size_bytes, -file_delta)
