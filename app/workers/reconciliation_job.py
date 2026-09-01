"""
Reconciliation job (Phase 9) — a one-shot Postgres<->GCS consistency check.

Run with: `python -m app.workers.reconciliation_job`

Deliberately NOT a `BaseWorker` subscriber and NOT a long-running loop
like `outbox_publisher.py`. It has no queue to drain and no reason to stay
resident between runs, so it is shaped as a batch job meant to be invoked
on a schedule by a Kubernetes CronJob (`k8s/22-cronjob-reconciliation.yaml`)
rather than a Deployment — same reasoning `outbox_publisher.py`'s
docstring applies to *its* own process boundary, one level further: this
job's natural replica count isn't even "one process running forever," it's
"one process, periodically."

Exit code is the machine-readable half of the contract: `0` when the scan
completed and found nothing, `1` when it completed and found real issues,
`2` when the scan itself could not finish (Postgres/GCS unreachable, or
truncated by RECONCILIATION_MAX_ISSUES). A CronJob's alerting hooks off
`kubectl get jobs` / exit status, not off parsing log lines.

This process makes exactly one commit-free read pass. It has no INSERT,
UPDATE, or DELETE statement anywhere in its call graph — see
ReconciliationService's module docstring for why that boundary is a
design decision, not an oversight.
"""

import asyncio
import sys

from app.core.config import get_settings
from app.database.gcs import get_storage_client
from app.database.session import AsyncSessionLocal
from app.logging.logger import configure_logging, get_logger
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.services.reconciliation_service import ReconciliationReport, ReconciliationService
from app.services.storage_service import StorageService

logger = get_logger(__name__)


async def run_once() -> ReconciliationReport:
    settings = get_settings()
    async with AsyncSessionLocal() as session:
        file_repository = FileMetadataRepository(session)
        storage_service = StorageService(get_storage_client(), settings.GCS_BUCKET_NAME)
        service = ReconciliationService(file_repository, storage_service, settings)
        return await service.run()


async def main_async() -> int:
    settings = get_settings()
    if not settings.RECONCILIATION_ENABLED:
        logger.info("reconciliation_job_disabled")
        return 0

    logger.info("reconciliation_job_started", batch_size=settings.RECONCILIATION_BATCH_SIZE)
    try:
        report = await run_once()
    except Exception as exc:  # noqa: BLE001
        logger.error("reconciliation_job_failed", error=str(exc))
        return 2

    if report.truncated:
        return 2
    if not report.is_clean:
        logger.warning(
            "reconciliation_job_found_issues",
            rows_scanned=report.rows_scanned,
            issue_count=len(report.issues),
        )
        return 1

    logger.info("reconciliation_job_clean", rows_scanned=report.rows_scanned)
    return 0


def main() -> None:  # pragma: no cover - process entrypoint
    configure_logging()
    exit_code = asyncio.run(main_async())
    sys.exit(exit_code)


if __name__ == "__main__":  # pragma: no cover
    main()
