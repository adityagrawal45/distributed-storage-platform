"""
Reconciliation service (Phase 9).

Two systems of record can drift apart even when every individual write
path is correct, because a *row* and an *object* are never written
atomically across Postgres and GCS — see README §10/§13 for why upload
rollback and Compose-then-verify exist at all. Reconciliation is the
independent, out-of-band check that catches the drift those in-line
safeguards didn't (a process killed at exactly the wrong instant, a
manual `gsutil rm`, a botched migration).

Scope of this phase, deliberately narrow:
- Detects `METADATA_WITHOUT_OBJECT` — a non-deleted, upload-completed
  `FileMetadata` row whose `object_name` does not exist in GCS. This is
  the dangerous direction: a user sees a file that 404s the instant they
  try to download it.
- Does NOT detect `OBJECT_WITHOUT_METADATA` (an orphaned GCS object with
  no owning row) in this phase. That direction requires listing the
  entire bucket (`list_blobs`), which the real `StorageService`/GCS SDK
  supports but this codebase has never needed and the fakes don't model
  — see docs/disaster-recovery.md "Reconciliation" for why that's a
  clearly-scoped-out follow-up rather than a silent gap: an orphaned
  object costs storage money, not correctness, so it is the lower-risk
  half of the problem to leave for later.
- NEVER deletes or mutates anything, in any mode. This service is
  read-only end to end. A future phase that wants to *act* on a finding
  (quarantine, re-upload, delete) is a deliberate, separate, reviewed
  change — not a flag on this one.
"""

import uuid
from dataclasses import dataclass, field
from enum import Enum

from app.core.config.settings import Settings
from app.exceptions.custom_exceptions import StorageObjectNotFoundException
from app.logging.logger import get_logger
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.services.storage_service import StorageService

logger = get_logger(__name__)


class ReconciliationIssueType(str, Enum):
    METADATA_WITHOUT_OBJECT = "metadata_without_object"


@dataclass
class ReconciliationIssue:
    issue_type: ReconciliationIssueType
    file_id: uuid.UUID
    owner_id: uuid.UUID
    object_name: str
    detail: str


@dataclass
class ReconciliationReport:
    rows_scanned: int = 0
    issues: list[ReconciliationIssue] = field(default_factory=list)
    truncated: bool = False
    """True if RECONCILIATION_MAX_ISSUES was hit before the scan finished —
    the report is a valid partial result, not a failure, but the caller
    should re-run rather than treat an empty next page as "all clean"."""

    @property
    def is_clean(self) -> bool:
        return not self.issues


class ReconciliationService:
    """Read-only Postgres<->GCS consistency check. See module docstring."""

    def __init__(
        self,
        file_repository: FileMetadataRepository,
        storage_service: StorageService,
        settings: Settings,
    ):
        self._file_repository = file_repository
        self._storage_service = storage_service
        self._settings = settings

    async def run(self) -> ReconciliationReport:
        report = ReconciliationReport()
        after_id: uuid.UUID | None = None
        batch_size = self._settings.RECONCILIATION_BATCH_SIZE
        max_issues = self._settings.RECONCILIATION_MAX_ISSUES

        while True:
            batch = await self._file_repository.list_completed_batch(after_id=after_id, limit=batch_size)
            if not batch:
                break

            for row in batch:
                report.rows_scanned += 1
                await self._check_row(row, report)
                if len(report.issues) >= max_issues:
                    report.truncated = True
                    logger.warning(
                        "reconciliation_truncated",
                        max_issues=max_issues,
                        rows_scanned=report.rows_scanned,
                    )
                    return report

            after_id = batch[-1].id

        logger.info(
            "reconciliation_completed",
            rows_scanned=report.rows_scanned,
            issues_found=len(report.issues),
        )
        return report

    async def _check_row(self, row, report: ReconciliationReport) -> None:
        try:
            await self._storage_service.get_blob_metadata(row.object_name)
        except StorageObjectNotFoundException:
            issue = ReconciliationIssue(
                issue_type=ReconciliationIssueType.METADATA_WITHOUT_OBJECT,
                file_id=row.id,
                owner_id=row.owner_id,
                object_name=row.object_name,
                detail="FileMetadata row is upload_status=COMPLETED but the object is missing from GCS.",
            )
            report.issues.append(issue)
            logger.error(
                "reconciliation_issue_found",
                issue_type=issue.issue_type.value,
                file_id=str(issue.file_id),
                object_name=issue.object_name,
            )
        # Any other exception (timeout, permission, transient GCS error) is
        # deliberately NOT caught here — it must abort the run rather than
        # be silently recorded as "the object is missing," which would be a
        # false positive that could trigger an unwarranted incident.
