"""
Phase 8 — service-layer outbox emission, driven through the real HTTP API.

These are the tests that prove the *producer* half of the architecture:
that every one of the nine documented hook points writes exactly one
`OutboxEvent` row, into the same transaction as the business data, with
the right aggregate and payload — and that a service constructed WITHOUT
an outbox emits nothing at all, which is the backward-compatibility
guarantee that let all 248 pre-Phase-8 tests pass unmodified.

The `client` fixture's `get_db` override yields the test's `db_session`
without committing, so the outbox rows written during a request are
directly queryable from that same session afterwards — which is exactly
the property under test (same session => same transaction => atomic).
"""

import io
import uuid

from sqlalchemy import select

from app.events.envelope import EventType
from app.models.outbox_event import OutboxEvent, OutboxEventStatus
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.repositories.folder_repository import FolderRepository
from app.services.folder_service import FolderService


async def _events(db_session, event_type: EventType | None = None) -> list[OutboxEvent]:
    statement = select(OutboxEvent).order_by(OutboxEvent.created_at)
    if event_type is not None:
        statement = statement.where(OutboxEvent.event_type == event_type.value)
    result = await db_session.execute(statement)
    return list(result.scalars().all())


async def _create_folder(authed_client, name="Docs", parent=None) -> dict:
    response = await authed_client.post(
        "/api/v1/folders", json={"name": name, "parent_folder_id": parent}
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _upload(authed_client, filename="photo.txt", content=b"hello world", folder_id=None) -> dict:
    data = {"folder_id": folder_id} if folder_id else {}
    response = await authed_client.post(
        "/api/v1/files/upload",
        files={"file": (filename, io.BytesIO(content), "text/plain")},
        data=data,
    )
    assert response.status_code == 201, response.text
    # `POST /files/upload` wraps the metadata under `data.file` alongside
    # `data.is_duplicate` — see FileUploadResponse.
    return response.json()["data"]["file"]


# ---------------------------------------------------------------------
# folder.created / folder.deleted
# ---------------------------------------------------------------------
async def test_creating_a_folder_emits_folder_created(authed_client, db_session):
    folder = await _create_folder(authed_client, "Reports")

    events = await _events(db_session, EventType.FOLDER_CREATED)
    assert len(events) == 1
    event = events[0]
    assert event.status == OutboxEventStatus.PENDING
    assert event.aggregate_type == "folder"
    assert str(event.aggregate_id) == folder["id"]
    assert event.payload["name"] == "Reports"
    assert event.payload["path"]


async def test_deleting_a_folder_emits_one_event_for_the_subtree_not_one_per_descendant(
    authed_client, db_session
):
    """
    A recursive soft-delete is ONE user intent. Fanning out per descendant
    would publish thousands of messages describing a single action.
    """
    parent = await _create_folder(authed_client, "Parent")
    await _create_folder(authed_client, "ChildA", parent["id"])
    await _create_folder(authed_client, "ChildB", parent["id"])

    response = await authed_client.delete(f"/api/v1/folders/{parent['id']}")
    assert response.status_code == 200, response.text

    events = await _events(db_session, EventType.FOLDER_DELETED)
    assert len(events) == 1
    assert str(events[0].aggregate_id) == parent["id"]
    assert events[0].payload["descendant_count"] == 2
    assert len(events[0].payload["descendant_folder_ids"]) == 2
    assert events[0].payload["soft_delete"] is True


# ---------------------------------------------------------------------
# file.uploaded / file.version.created
# ---------------------------------------------------------------------
async def test_uploading_a_file_emits_file_uploaded_with_storage_coordinates(authed_client, db_session):
    file = await _upload(authed_client, "picture.txt", b"some bytes here")

    events = await _events(db_session, EventType.FILE_UPLOADED)
    assert len(events) == 1
    event = events[0]
    assert event.aggregate_type == "file"
    assert str(event.aggregate_id) == file["id"]
    # The consumer needs enough to find the bytes without another API call.
    assert event.payload["object_name"]
    assert event.payload["bucket_name"]
    assert event.payload["content_type"]
    assert event.payload["size"] == len(b"some bytes here")
    assert event.payload["checksum"]


async def test_replacing_a_file_emits_file_version_created(authed_client, db_session):
    file = await _upload(authed_client, "doc.txt", b"v1 content")

    response = await authed_client.put(
        f"/api/v1/files/{file['id']}/replace",
        files={"file": ("doc.txt", io.BytesIO(b"v2 content is longer"), "text/plain")},
    )
    assert response.status_code == 200, response.text

    events = await _events(db_session, EventType.FILE_VERSION_CREATED)
    assert len(events) == 1
    assert events[0].payload["version"] == 2
    assert events[0].payload["previous_object_name"]
    assert events[0].payload["object_name"] != events[0].payload["previous_object_name"]


# ---------------------------------------------------------------------
# file.renamed / file.moved / file.deleted / file.restored
# ---------------------------------------------------------------------
async def test_renaming_a_file_emits_file_renamed_with_both_names(authed_client, db_session):
    file = await _upload(authed_client, "before.txt")

    response = await authed_client.post(
        f"/api/v1/metadata/{file['id']}/rename", json={"name": "after.txt"}
    )
    assert response.status_code == 200, response.text

    events = await _events(db_session, EventType.FILE_RENAMED)
    assert len(events) == 1
    assert events[0].payload["old_filename"] == "before.txt"
    assert events[0].payload["new_filename"] == "after.txt"


async def test_moving_a_file_emits_file_moved_with_both_folders(authed_client, db_session):
    folder = await _create_folder(authed_client, "Destination")
    file = await _upload(authed_client, "movable.txt")

    response = await authed_client.post(
        f"/api/v1/metadata/{file['id']}/move", json={"new_folder_id": folder["id"]}
    )
    assert response.status_code == 200, response.text

    events = await _events(db_session, EventType.FILE_MOVED)
    assert len(events) == 1
    assert events[0].payload["old_folder_id"] is None
    assert events[0].payload["new_folder_id"] == folder["id"]


async def test_trashing_and_restoring_a_file_emit_deleted_then_restored(authed_client, db_session):
    file = await _upload(authed_client, "trashme.txt")

    assert (await authed_client.delete(f"/api/v1/metadata/{file['id']}")).status_code == 200
    assert (await authed_client.post(f"/api/v1/metadata/{file['id']}/restore")).status_code == 200

    deleted = await _events(db_session, EventType.FILE_DELETED)
    restored = await _events(db_session, EventType.FILE_RESTORED)
    assert len(deleted) == 1
    assert len(restored) == 1
    # Soft delete: bytes are untouched and the file is recoverable, so the
    # event must say so rather than reading as a purge.
    assert deleted[0].payload["soft_delete"] is True


async def test_updating_metadata_content_emits_a_version_event_but_a_no_op_update_does_not(
    authed_client, db_session
):
    file = await _upload(authed_client, "meta.txt", b"abcdef")

    # mime_type-only change: no new version, therefore no version event.
    response = await authed_client.put(
        f"/api/v1/metadata/{file['id']}", json={"mime_type": "text/markdown"}
    )
    assert response.status_code == 200, response.text
    assert await _events(db_session, EventType.FILE_VERSION_CREATED) == []

    # A checksum change IS a new version.
    response = await authed_client.put(
        f"/api/v1/metadata/{file['id']}", json={"checksum": "f" * 64, "size": 99}
    )
    assert response.status_code == 200, response.text
    events = await _events(db_session, EventType.FILE_VERSION_CREATED)
    assert len(events) == 1
    assert events[0].payload["version"] == 2


# ---------------------------------------------------------------------
# Envelope/correlation plumbing
# ---------------------------------------------------------------------
async def test_the_outbox_row_inherits_the_requests_correlation_id(authed_client, db_session):
    """
    Correlation is read from `structlog.contextvars`, which
    `RequestContextMiddleware` binds per request — so a worker's log lines
    join the user's original HTTP request with no parameter threading.
    """
    correlation_id = str(uuid.uuid4())
    response = await authed_client.post(
        "/api/v1/folders",
        json={"name": "Correlated", "parent_folder_id": None},
        headers={"X-Correlation-ID": correlation_id},
    )
    assert response.status_code == 201

    events = await _events(db_session, EventType.FOLDER_CREATED)
    assert str(events[0].correlation_id) == correlation_id


async def test_a_non_uuid_correlation_header_does_not_break_the_upload(authed_client, db_session):
    """
    `RequestContextMiddleware` honors an arbitrary client-supplied
    `X-Correlation-ID` string verbatim. Emitting must tolerate that
    rather than 500 the user's upload over a malformed header.
    """
    response = await authed_client.post(
        "/api/v1/folders",
        json={"name": "Weird", "parent_folder_id": None},
        headers={"X-Correlation-ID": "not-a-uuid-at-all"},
    )
    assert response.status_code == 201

    events = await _events(db_session, EventType.FOLDER_CREATED)
    assert len(events) == 1
    assert isinstance(events[0].correlation_id, uuid.UUID)


async def test_every_emitted_row_starts_pending_and_immediately_due(authed_client, db_session):
    await _upload(authed_client, "due.txt")

    events = await _events(db_session)
    assert events
    for event in events:
        assert event.status == OutboxEventStatus.PENDING
        assert event.attempt_count == 0
        assert event.published_at is None
        assert event.event_version == 1
        # A brand-new row must be eligible on the very next poll.
        assert event.next_attempt_at is not None


async def test_event_ids_are_unique_across_a_multi_event_request(authed_client, db_session):
    await _create_folder(authed_client, "A")
    await _create_folder(authed_client, "B")
    await _upload(authed_client, "c.txt")

    events = await _events(db_session)
    assert len({e.event_id for e in events}) == len(events)


# ---------------------------------------------------------------------
# The backward-compatibility guarantee
# ---------------------------------------------------------------------
async def test_a_service_built_without_an_outbox_emits_nothing(db_session):
    """
    THE reason all 248 pre-Phase-8 tests pass unmodified: `outbox` is a
    keyword-only parameter defaulting to None, and unset means no-op.
    This is the same technique Phase 7 used for `cache=`/`invalidator=`.
    """
    service = FolderService(FolderRepository(db_session))
    assert service._events_enabled is False

    owner_id = uuid.uuid4()
    from app.models.user import User

    db_session.add(
        User(
            id=owner_id,
            first_name="No",
            last_name="Events",
            email=f"{owner_id}@nimbusfs.io",
            hashed_password="x",
        )
    )
    await db_session.flush()

    await service.create_folder(owner_id, "Silent", None)
    assert await _events(db_session) == []


async def test_emitting_never_raises_even_if_the_repository_fails(db_session):
    """
    Emit failure must not fail the user's request. (Note: a rolled-back
    TRANSACTION correctly takes the outbox row with it — that is the
    point of the pattern and is not what is swallowed here. What is
    swallowed is a construction-level failure.)
    """

    class ExplodingOutbox:
        async def add_event(self, **kwargs):
            raise RuntimeError("simulated repository failure")

    service = FolderService(FolderRepository(db_session), outbox=ExplodingOutbox())
    envelope = await service._emit_event(
        EventType.FOLDER_CREATED,
        aggregate_type="folder",
        aggregate_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        payload={"folder_id": "x"},
    )
    assert envelope is None  # logged at ERROR, not raised


async def test_outbox_rows_roll_back_with_the_business_transaction(authed_client, db_session):
    """
    The atomicity property, end to end: roll the transaction back and the
    file row AND its event row both disappear. If the outbox committed
    independently, a consumer would act on a file that never existed.
    """
    file = await _upload(authed_client, "atomic.txt")
    assert len(await _events(db_session, EventType.FILE_UPLOADED)) == 1

    await db_session.rollback()

    files = FileMetadataRepository(db_session)
    assert await files.get_by_id(uuid.UUID(file["id"])) is None
    assert await _events(db_session) == []
