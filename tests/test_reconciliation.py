"""
Phase 9 — reconciliation service tests.

Covers the one direction this phase's `ReconciliationService` actually
checks: a `FileMetadata` row that claims `upload_status=COMPLETED` but
whose object is missing from GCS (see the service's module docstring for
why the inverse direction — orphaned objects with no owning row — is
explicitly out of scope this phase).

No test here ever asserts a delete happened, because the service has no
code path that performs one — that absence is itself the property under
test in `test_never_mutates_or_deletes_anything`.
"""

import io
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.database.session import Base
from app.models.file_metadata import FileMetadata, FileStatus, UploadStatus
from app.models.user import User
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.services.reconciliation_service import ReconciliationIssueType, ReconciliationService
from app.services.storage_service import StorageService
from tests.fakes.fake_gcs import FakeGCSClient

BUCKET = get_settings().GCS_BUCKET_NAME


@pytest.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


@pytest.fixture
def gcs() -> FakeGCSClient:
    return FakeGCSClient()


def put_object(gcs: FakeGCSClient, name: str, data: bytes = b"hello") -> None:
    gcs.bucket(BUCKET).blob(name).upload_from_file(io.BytesIO(data), content_type="text/plain", size=len(data))


async def seed_row(
    factory,
    *,
    object_name: str,
    upload_status: UploadStatus = UploadStatus.COMPLETED,
    is_deleted: bool = False,
) -> uuid.UUID:
    file_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            User(
                id=owner_id,
                first_name="Recon",
                last_name="Owner",
                email=f"{owner_id}@nimbusfs.io",
                hashed_password="x",
            )
        )
        session.add(
            FileMetadata(
                id=file_id,
                owner_id=owner_id,
                original_filename="doc.txt",
                stored_filename=f"{file_id}.txt",
                extension="txt",
                mime_type="text/plain",
                size=5,
                checksum="c" * 64,
                version=1,
                status=FileStatus.ACTIVE,
                bucket_name=BUCKET,
                object_name=object_name,
                upload_status=upload_status,
                is_deleted=is_deleted,
                created_by=owner_id,
                updated_by=owner_id,
            )
        )
        await session.commit()
    return file_id


async def run_reconciliation(factory, gcs: FakeGCSClient) -> tuple:
    settings = get_settings()
    async with factory() as session:
        repo = FileMetadataRepository(session)
        storage = StorageService(gcs, BUCKET)
        service = ReconciliationService(repo, storage, settings)
        report = await service.run()
    return report


@pytest.mark.asyncio
async def test_clean_state_reports_no_issues(db_factory, gcs):
    object_name = "tenant/owner/2026/09/present.txt"
    put_object(gcs, object_name)
    await seed_row(db_factory, object_name=object_name)

    report = await run_reconciliation(db_factory, gcs)

    assert report.rows_scanned == 1
    assert report.is_clean
    assert report.issues == []


@pytest.mark.asyncio
async def test_missing_object_is_flagged(db_factory, gcs):
    # Deliberately never uploaded to the fake bucket.
    object_name = "tenant/owner/2026/09/missing.txt"
    file_id = await seed_row(db_factory, object_name=object_name)

    report = await run_reconciliation(db_factory, gcs)

    assert report.rows_scanned == 1
    assert not report.is_clean
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.issue_type == ReconciliationIssueType.METADATA_WITHOUT_OBJECT
    assert issue.file_id == file_id
    assert issue.object_name == object_name


@pytest.mark.asyncio
async def test_soft_deleted_rows_are_skipped(db_factory, gcs):
    # No object uploaded, but the row is soft-deleted — a deleted file
    # legitimately has no live object requirement, so this must NOT flag.
    object_name = "tenant/owner/2026/09/trashed.txt"
    await seed_row(db_factory, object_name=object_name, is_deleted=True)

    report = await run_reconciliation(db_factory, gcs)

    assert report.rows_scanned == 0
    assert report.is_clean


@pytest.mark.asyncio
async def test_pending_upload_rows_are_skipped(db_factory, gcs):
    # A row still mid-upload is expected to have no object yet — that is
    # a known, transient state, not a reconciliation issue.
    object_name = "tenant/owner/2026/09/pending.txt"
    await seed_row(db_factory, object_name=object_name, upload_status=UploadStatus.PENDING)

    report = await run_reconciliation(db_factory, gcs)

    assert report.rows_scanned == 0
    assert report.is_clean


@pytest.mark.asyncio
async def test_pagination_walks_every_row_across_multiple_batches(db_factory, gcs):
    settings = get_settings()
    settings.RECONCILIATION_BATCH_SIZE = 2  # force multiple pages over 5 rows
    try:
        missing_ids = set()
        for i in range(5):
            object_name = f"tenant/owner/2026/09/file-{i}.txt"
            if i % 2 == 0:
                put_object(gcs, object_name)
            else:
                missing_ids.add(await seed_row(db_factory, object_name=object_name))
                continue
            await seed_row(db_factory, object_name=object_name)

        report = await run_reconciliation(db_factory, gcs)

        assert report.rows_scanned == 5
        assert {issue.file_id for issue in report.issues} == missing_ids
    finally:
        settings.RECONCILIATION_BATCH_SIZE = 500


@pytest.mark.asyncio
async def test_never_mutates_or_deletes_anything(db_factory, gcs):
    """
    The service has no delete/update code path at all — this test proves
    it by re-reading the exact same row after a run that found it as an
    issue and confirming nothing about it changed.
    """
    object_name = "tenant/owner/2026/09/missing.txt"
    file_id = await seed_row(db_factory, object_name=object_name)

    await run_reconciliation(db_factory, gcs)

    async with db_factory() as session:
        repo = FileMetadataRepository(session)
        row = await repo.get_by_id(file_id)
        assert row is not None
        assert row.is_deleted is False
        assert row.upload_status == UploadStatus.COMPLETED
        assert row.object_name == object_name
