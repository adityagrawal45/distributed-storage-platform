"""
Phase 8 — thumbnail service + worker tests.

Real Pillow, real bytes: the fixtures below generate genuine JPEG/PNG/
WebP/GIF images in memory and the assertions decode the produced
thumbnail back. Mocking Pillow here would test nothing — the entire risk
in this component is what a decoder does with real (and deliberately
malformed) input.

The security-shaped tests are the ones that matter most:
unsupported types must be rejected BEFORE any decode is attempted, and a
malformed/mislabelled file must fail one message rather than crash the
worker.
"""

import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.database.session import Base
from app.events.envelope import EventEnvelope, EventType
from app.exceptions.custom_exceptions import NonRetryableEventError, RetryableEventError
from app.models.file_metadata import FileMetadata, FileStatus, UploadStatus
from app.models.processed_event import ProcessedEvent, ProcessedEventStatus
from app.models.user import User
from app.services.storage_service import StorageService
from app.services.thumbnail_service import ThumbnailService
from app.workers.thumbnail_worker import ThumbnailWorker
from tests.fakes.fake_gcs import FakeGCSClient
from tests.fakes.fake_pubsub import FakeMessage

BUCKET = get_settings().GCS_BUCKET_NAME


def make_image_bytes(fmt: str, size=(800, 600), mode="RGB") -> bytes:
    image = Image.new(mode, size, color=(120, 60, 200) if mode == "RGB" else 255)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture
async def worker_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


@pytest.fixture
def gcs() -> FakeGCSClient:
    return FakeGCSClient()


def put_object(gcs: FakeGCSClient, name: str, data: bytes, content_type: str) -> None:
    blob = gcs.bucket(BUCKET).blob(name)
    blob.upload_from_file(io.BytesIO(data), content_type=content_type, size=len(data))


async def seed_file(factory, *, object_name: str, content_type: str) -> uuid.UUID:
    file_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            User(
                id=owner_id,
                first_name="Thumb",
                last_name="Owner",
                email=f"{owner_id}@nimbusfs.io",
                hashed_password="x",
            )
        )
        session.add(
            FileMetadata(
                id=file_id,
                owner_id=owner_id,
                original_filename="photo.png",
                stored_filename=f"{file_id}.png",
                extension="png",
                mime_type=content_type,
                size=1,
                checksum="c" * 64,
                version=1,
                status=FileStatus.ACTIVE,
                bucket_name=BUCKET,
                object_name=object_name,
                upload_status=UploadStatus.COMPLETED,
                created_by=owner_id,
                updated_by=owner_id,
            )
        )
        await session.commit()
    return file_id


def _request(file_id, object_name, content_type="image/png") -> EventEnvelope:
    return EventEnvelope(
        event_type=EventType.THUMBNAIL_REQUESTED,
        producer="file-processing-worker",
        user_id=uuid.uuid4(),
        payload={
            "file_id": str(file_id),
            "object_name": object_name,
            "content_type": content_type,
            "bucket_name": BUCKET,
        },
    )


def _worker(worker_db, gcs) -> ThumbnailWorker:
    return ThumbnailWorker(
        thumbnail_service=ThumbnailService(StorageService(gcs)),
        session_factory=worker_db,
        subscription="thumb-sub",
    )


async def _processed(factory) -> list[ProcessedEvent]:
    async with factory() as session:
        result = await session.execute(select(ProcessedEvent))
        return list(result.scalars().all())


async def _file(factory, file_id) -> FileMetadata:
    async with factory() as session:
        result = await session.execute(select(FileMetadata).where(FileMetadata.id == file_id))
        return result.scalar_one()


# ---------------------------------------------------------------------
# ThumbnailService — the allow-list
# ---------------------------------------------------------------------
@pytest.mark.parametrize("content_type", ["image/jpeg", "image/png", "image/webp", "image/gif"])
def test_the_four_supported_types_are_accepted(gcs, content_type):
    service = ThumbnailService(StorageService(gcs))
    assert service.assert_supported(content_type) == content_type


@pytest.mark.parametrize(
    "content_type",
    ["application/pdf", "video/mp4", "image/tiff", "image/svg+xml", "image/bmp", "text/plain", "", None],
)
def test_every_other_type_is_rejected_as_permanently_unprocessable(gcs, content_type):
    """
    Not merely "returns False" — it must raise the NON-retryable error,
    because that is what makes the worker ACK-and-record instead of
    NACK-and-retry forever.
    """
    service = ThumbnailService(StorageService(gcs))
    with pytest.raises(NonRetryableEventError):
        service.assert_supported(content_type)


def test_the_type_check_is_case_insensitive(gcs):
    assert ThumbnailService(StorageService(gcs)).assert_supported("IMAGE/PNG") == "image/png"


async def test_an_unsupported_type_is_rejected_before_any_download_or_decode(gcs, worker_db):
    """
    The security property: Pillow is never handed bytes of a type this
    build did not sign up for. Nothing is even fetched from storage.
    """

    class TripwireStorage(StorageService):
        def __init__(self):
            super().__init__(gcs)
            self.touched = False

        async def get_blob_metadata(self, object_name):
            self.touched = True
            return await super().get_blob_metadata(object_name)

    storage = TripwireStorage()
    service = ThumbnailService(storage)
    with pytest.raises(NonRetryableEventError):
        await service.generate(file_id=uuid.uuid4(), object_name="x", content_type="application/pdf")
    assert storage.touched is False


# ---------------------------------------------------------------------
# ThumbnailService — real rendering
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("fmt", "content_type"),
    [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp"), ("GIF", "image/gif")],
)
async def test_each_supported_format_renders_a_real_png_thumbnail(gcs, fmt, content_type):
    name = f"src/{fmt.lower()}"
    put_object(gcs, name, make_image_bytes(fmt), content_type)
    service = ThumbnailService(StorageService(gcs))
    file_id = uuid.uuid4()

    result = await service.generate(file_id=file_id, object_name=name, content_type=content_type)

    assert result.object_name == f"thumbnails/{file_id}.png"
    assert result.content_type == "image/png"
    stored = gcs.bucket(BUCKET).blob(result.object_name).download_as_bytes()
    with Image.open(io.BytesIO(stored)) as thumbnail:
        assert thumbnail.format == "PNG"  # always PNG regardless of input format
        assert max(thumbnail.size) <= get_settings().THUMBNAIL_MAX_DIMENSION_PX


async def test_aspect_ratio_is_preserved(gcs):
    put_object(gcs, "wide", make_image_bytes("PNG", size=(1600, 400)), "image/png")
    service = ThumbnailService(StorageService(gcs))

    result = await service.generate(file_id=uuid.uuid4(), object_name="wide", content_type="image/png")

    assert result.width / result.height == pytest.approx(4.0, rel=0.05)
    assert result.width <= get_settings().THUMBNAIL_MAX_DIMENSION_PX


async def test_a_small_image_is_never_upscaled(gcs):
    """`thumbnail()` fits inside a box and never enlarges — `resize()` would."""
    put_object(gcs, "tiny", make_image_bytes("PNG", size=(64, 48)), "image/png")
    service = ThumbnailService(StorageService(gcs))

    result = await service.generate(file_id=uuid.uuid4(), object_name="tiny", content_type="image/png")

    assert (result.width, result.height) == (64, 48)


async def test_the_thumbnail_object_name_is_deterministic_so_regeneration_overwrites(gcs):
    put_object(gcs, "src", make_image_bytes("PNG"), "image/png")
    service = ThumbnailService(StorageService(gcs))
    file_id = uuid.uuid4()

    first = await service.generate(file_id=file_id, object_name="src", content_type="image/png")
    second = await service.generate(file_id=file_id, object_name="src", content_type="image/png")

    assert first.object_name == second.object_name
    # One object, not two — no orphan accumulation.
    assert len([k for k in gcs.bucket(BUCKET).store if k.startswith("thumbnails/")]) == 1


async def test_corrupt_bytes_are_a_permanent_failure_not_a_crash(gcs):
    """A truncated/garbage file must fail ONE message, never take the worker down."""
    put_object(gcs, "corrupt", b"\x89PNG\r\n\x1a\n" + b"garbage" * 20, "image/png")
    service = ThumbnailService(StorageService(gcs))

    with pytest.raises(NonRetryableEventError):
        await service.generate(file_id=uuid.uuid4(), object_name="corrupt", content_type="image/png")


async def test_a_file_lying_about_its_format_is_rejected_rather_than_sniffed(gcs):
    """`Image.open(formats=...)` pins the decoder to the declared type."""
    put_object(gcs, "liar", make_image_bytes("PNG"), "image/jpeg")
    service = ThumbnailService(StorageService(gcs))

    with pytest.raises(NonRetryableEventError):
        await service.generate(file_id=uuid.uuid4(), object_name="liar", content_type="image/jpeg")


async def test_an_empty_object_is_a_permanent_failure(gcs):
    put_object(gcs, "empty", b"", "image/png")
    service = ThumbnailService(StorageService(gcs))

    with pytest.raises(NonRetryableEventError):
        await service.generate(file_id=uuid.uuid4(), object_name="empty", content_type="image/png")


async def test_a_storage_read_failure_is_retryable_not_permanent(gcs):
    """Infrastructure failures NACK; content failures ACK. Getting this backwards drops real work."""
    service = ThumbnailService(StorageService(gcs))
    with pytest.raises(RetryableEventError):
        await service.generate(
            file_id=uuid.uuid4(), object_name="never/uploaded", content_type="image/png"
        )


# ---------------------------------------------------------------------
# ThumbnailWorker
# ---------------------------------------------------------------------
async def test_the_worker_generates_a_thumbnail_and_records_it_on_the_file_row(worker_db, gcs):
    put_object(gcs, "src/photo.png", make_image_bytes("PNG"), "image/png")
    file_id = await seed_file(worker_db, object_name="src/photo.png", content_type="image/png")
    message = FakeMessage(_request(file_id, "src/photo.png").to_json_bytes())

    await _worker(worker_db, gcs)._handle(message)

    assert message.acked is True
    file = await _file(worker_db, file_id)
    assert file.thumbnail_object_name == f"thumbnails/{file_id}.png"
    assert gcs.bucket(BUCKET).blob(file.thumbnail_object_name).exists()
    [row] = await _processed(worker_db)
    assert row.status == ProcessedEventStatus.SUCCEEDED


async def test_the_worker_acks_an_unsupported_type_with_a_failed_row_and_no_thumbnail(worker_db, gcs):
    put_object(gcs, "src/doc.pdf", b"%PDF-1.7 not an image", "application/pdf")
    file_id = await seed_file(worker_db, object_name="src/doc.pdf", content_type="application/pdf")
    message = FakeMessage(_request(file_id, "src/doc.pdf", "application/pdf").to_json_bytes())

    await _worker(worker_db, gcs)._handle(message)

    assert message.acked is True
    assert message.nacked is False
    assert (await _file(worker_db, file_id)).thumbnail_object_name is None
    [row] = await _processed(worker_db)
    assert row.status == ProcessedEventStatus.FAILED
    assert "Unsupported content type" in row.error


async def test_a_deleted_file_is_a_permanent_failure(worker_db, gcs):
    """A real race with a permanent answer — there is nothing to attach a thumbnail to."""
    put_object(gcs, "src/gone.png", make_image_bytes("PNG"), "image/png")
    message = FakeMessage(_request(uuid.uuid4(), "src/gone.png").to_json_bytes())

    await _worker(worker_db, gcs)._handle(message)

    assert message.acked is True
    [row] = await _processed(worker_db)
    assert row.status == ProcessedEventStatus.FAILED
    assert "no longer exists" in row.error


async def test_a_missing_source_object_nacks_for_retry(worker_db, gcs):
    file_id = await seed_file(worker_db, object_name="src/absent.png", content_type="image/png")
    message = FakeMessage(_request(file_id, "src/absent.png").to_json_bytes())

    await _worker(worker_db, gcs)._handle(message)

    assert message.nacked is True
    assert await _processed(worker_db) == []


async def test_a_redelivered_thumbnail_request_is_absorbed(worker_db, gcs):
    put_object(gcs, "src/dup.png", make_image_bytes("PNG"), "image/png")
    file_id = await seed_file(worker_db, object_name="src/dup.png", content_type="image/png")
    envelope = _request(file_id, "src/dup.png")
    worker = _worker(worker_db, gcs)

    await worker._handle(FakeMessage(envelope.to_json_bytes()))
    second = FakeMessage(envelope.to_json_bytes(), delivery_attempt=2)
    await worker._handle(second)

    assert second.acked is True
    assert len(await _processed(worker_db)) == 1


async def test_the_worker_ignores_non_thumbnail_events_on_the_shared_topic(worker_db, gcs):
    envelope = EventEnvelope(
        event_type=EventType.FILE_UPLOADED, user_id=uuid.uuid4(), payload={"file_id": str(uuid.uuid4())}
    )
    message = FakeMessage(envelope.to_json_bytes())

    await _worker(worker_db, gcs)._handle(message)

    assert message.acked is True
    assert await _processed(worker_db) == []


async def test_a_malformed_file_id_is_permanent(worker_db, gcs):
    envelope = EventEnvelope(
        event_type=EventType.THUMBNAIL_REQUESTED,
        user_id=uuid.uuid4(),
        payload={"file_id": "not-a-uuid", "object_name": "x", "content_type": "image/png"},
    )
    message = FakeMessage(envelope.to_json_bytes())

    await _worker(worker_db, gcs)._handle(message)

    assert message.acked is True
    [row] = await _processed(worker_db)
    assert row.status == ProcessedEventStatus.FAILED
