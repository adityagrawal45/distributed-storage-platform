"""
Chunked / resumable upload routes (Phase 6).

Deliberately a separate router/prefix (`/uploads`) from
`/files` (Phase 3) and `/metadata` (Phase 2) — this is a distinct
lifecycle (multi-request, stateful-in-Postgres session) rather than a
single-request upload, and keeping it its own resource makes that
explicit in the API surface rather than overloading `/files/upload`
with two different request shapes.

No business logic lives here — every route is thin: authenticate,
delegate to `ChunkedUploadService`, wrap the result in `APIResponse[T]`.
See `app/services/chunked_upload_service.py` for the actual orchestration
and the full design-decision writeup (GCS architecture, concurrency
model, idempotency, checksum strategy).
"""

import uuid

from fastapi import APIRouter, Depends, Header, Path, Request, status
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.enums import UploadSessionStatus
from app.core.rate_limiter import RateLimitCategory
from app.dependencies.auth import CurrentUser
from app.dependencies.providers import ChunkedUploadServiceDep, IdempotencyServiceDep
from app.dependencies.rate_limit import rate_limit
from app.exceptions.custom_exceptions import ChunkSizeInvalidException
from app.schemas.file_metadata import FileMetadataRead
from app.schemas.response import APIResponse
from app.schemas.upload import (
    ChunkRead,
    ChunkUploadResponse,
    UploadCancelResponse,
    UploadCompleteResponse,
    UploadInitiateRequest,
    UploadInitiateResponse,
    UploadProgressRead,
)
from app.services.idempotency_service import IdempotencyOutcome, compute_fingerprint

router = APIRouter(prefix="/uploads", tags=["Chunked Uploads"])


def _progress_percentage(uploaded_bytes: int, total_size: int) -> float:
    if total_size <= 0:
        return 0.0
    return round(min(uploaded_bytes / total_size, 1.0) * 100, 2)


async def _read_body_bounded(request: Request, max_bytes: int) -> bytes:
    """
    Reads the raw request body via the ASGI stream in bounded pieces,
    aborting the instant the running total exceeds `max_bytes` — never
    trusts the client-declared `Content-Length` header alone (a
    malicious/buggy client could lie about it), and never buffers more
    than one chunk's worth (bounded by `Settings.CHUNK_MAX_SIZE_BYTES`)
    regardless of how large the underlying file is. This — not
    `UploadFile` — is deliberately used here: Starlette's `UploadFile`
    can fall back to spooling large uploads to a temp file on local
    disk, which this platform (stateless Kubernetes pods, no local
    persistence — Phase 4/5) must never do.
    """
    buffer = bytearray()
    async for piece in request.stream():
        buffer.extend(piece)
        if len(buffer) > max_bytes:
            raise ChunkSizeInvalidException(f"Chunk exceeds the maximum allowed chunk size of {max_bytes} bytes.")
    return bytes(buffer)


@router.post(
    "",
    response_model=APIResponse[UploadInitiateResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Start a new chunked/resumable upload session",
    # Phase 7: initiate and complete are rate-limited; the per-CHUNK PUT
    # deliberately is NOT. A single large upload legitimately issues
    # thousands of chunk PUTs in parallel (that is the entire point of
    # Phase 6), so a per-request budget there would throttle correct
    # behavior rather than abuse. Session creation and finalization are
    # the natural, low-cardinality choke points to limit instead.
    dependencies=[Depends(rate_limit(RateLimitCategory.UPLOAD_INITIATE))],
    description=(
        "Supports an optional `Idempotency-Key` header: retrying the same "
        "initiate call with the same key replays the original session "
        "instead of creating a second one."
    ),
)
async def initiate_upload(
    current_user: CurrentUser,
    upload_service: ChunkedUploadServiceDep,
    idempotency_service: IdempotencyServiceDep,
    body: UploadInitiateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> APIResponse[UploadInitiateResponse] | JSONResponse:
    if idempotency_key:
        fingerprint = compute_fingerprint(
            "chunked_upload_initiate", str(body.folder_id), body.filename, str(body.size), str(body.chunk_size)
        )
        check = await idempotency_service.check_or_begin(current_user.id, idempotency_key, fingerprint)
        if check.outcome is IdempotencyOutcome.REPLAY:
            return JSONResponse(status_code=check.cached_status_code, content=check.cached_body)

    try:
        session = await upload_service.initiate_upload(
            current_user.id,
            filename=body.filename,
            total_size=body.size,
            mime_type=body.mime_type,
            folder_id=body.folder_id,
            chunk_size=body.chunk_size,
            expected_checksum=body.checksum,
            idempotency_key=idempotency_key,
        )
    except Exception:
        if idempotency_key:
            await idempotency_service.fail(current_user.id, idempotency_key)
        raise

    response = APIResponse(
        message="Upload session created.",
        data=UploadInitiateResponse(
            upload_id=session.id,
            chunk_size=session.chunk_size,
            total_chunks=session.total_chunks,
            total_size=session.total_size,
            expires_at=session.expires_at,
            status=session.status,
        ),
    )

    if idempotency_key:
        await idempotency_service.complete(
            current_user.id, idempotency_key, fingerprint, status.HTTP_201_CREATED, response.model_dump(mode="json")
        )

    return response


@router.get(
    "/{upload_id}",
    response_model=APIResponse[UploadProgressRead],
    summary="Get upload session status and progress (drives resume behavior)",
)
async def get_upload_status(
    current_user: CurrentUser,
    upload_service: ChunkedUploadServiceDep,
    upload_id: uuid.UUID = Path(...),
) -> APIResponse[UploadProgressRead]:
    progress = await upload_service.get_progress(upload_id, current_user.id)
    session = progress.session
    data = UploadProgressRead(
        upload_id=session.id,
        status=session.status,
        filename=session.filename,
        total_size=session.total_size,
        chunk_size=session.chunk_size,
        total_chunks=session.total_chunks,
        uploaded_chunks=progress.uploaded_chunk_numbers,
        missing_chunks=progress.missing_chunk_numbers,
        uploaded_bytes=progress.uploaded_bytes,
        progress_percentage=_progress_percentage(progress.uploaded_bytes, session.total_size),
        expires_at=session.expires_at,
        created_at=session.created_at,
    )
    return APIResponse(message="Upload status retrieved.", data=data)


@router.get(
    "/{upload_id}/chunks",
    response_model=APIResponse[list[ChunkRead]],
    summary="List all chunk records for an upload session",
)
async def list_upload_chunks(
    current_user: CurrentUser,
    upload_service: ChunkedUploadServiceDep,
    upload_id: uuid.UUID = Path(...),
) -> APIResponse[list[ChunkRead]]:
    chunks = await upload_service.list_chunks(upload_id, current_user.id)
    return APIResponse(
        message="Chunks retrieved.", data=[ChunkRead.model_validate(chunk) for chunk in chunks]
    )


@router.put(
    "/{upload_id}/chunks/{chunk_number}",
    response_model=APIResponse[ChunkUploadResponse],
    summary="Upload a single chunk (safe to call in parallel and out of order; safe to retry)",
    description=(
        "The raw chunk bytes are the request body. Optionally set "
        "`X-Chunk-Checksum` to a SHA-256 hex digest for the server to "
        "verify before accepting the chunk. Chunks may be uploaded in "
        "any order and concurrently from any client connection/pod."
    ),
)
async def upload_chunk(
    request: Request,
    current_user: CurrentUser,
    upload_service: ChunkedUploadServiceDep,
    upload_id: uuid.UUID = Path(...),
    chunk_number: int = Path(..., ge=1),
    x_chunk_checksum: str | None = Header(default=None, alias="X-Chunk-Checksum"),
) -> APIResponse[ChunkUploadResponse]:
    settings = get_settings()
    data = await _read_body_bounded(request, settings.CHUNK_MAX_SIZE_BYTES)

    chunk = await upload_service.upload_chunk(
        upload_id, current_user.id, chunk_number, data, declared_checksum=x_chunk_checksum
    )
    progress = await upload_service.get_progress(upload_id, current_user.id)

    return APIResponse(
        message="Chunk uploaded successfully.",
        data=ChunkUploadResponse(
            upload_id=upload_id,
            chunk_number=chunk.chunk_number,
            status=chunk.status,
            size=chunk.size,
            checksum=chunk.checksum,
            uploaded_bytes=progress.uploaded_bytes,
            total_size=progress.session.total_size,
            progress_percentage=_progress_percentage(progress.uploaded_bytes, progress.session.total_size),
        ),
    )


@router.post(
    "/{upload_id}/complete",
    response_model=APIResponse[UploadCompleteResponse],
    summary="Finalize an upload: compose chunks, verify integrity, create the file",
    dependencies=[Depends(rate_limit(RateLimitCategory.UPLOAD_COMPLETE))],
    description=(
        "Safe against duplicate requests: calling this again after a "
        "successful completion returns the same result rather than "
        "erroring or redoing work. Supports an optional `Idempotency-Key` "
        "header for the same safe-retry contract as `POST /files/upload`."
    ),
)
async def complete_upload(
    current_user: CurrentUser,
    upload_service: ChunkedUploadServiceDep,
    idempotency_service: IdempotencyServiceDep,
    upload_id: uuid.UUID = Path(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> APIResponse[UploadCompleteResponse] | JSONResponse:
    if idempotency_key:
        fingerprint = compute_fingerprint("chunked_upload_complete", str(upload_id))
        check = await idempotency_service.check_or_begin(current_user.id, idempotency_key, fingerprint)
        if check.outcome is IdempotencyOutcome.REPLAY:
            return JSONResponse(status_code=check.cached_status_code, content=check.cached_body)

    try:
        file = await upload_service.complete_upload(upload_id, current_user.id, current_user.id)
    except Exception:
        if idempotency_key:
            await idempotency_service.fail(current_user.id, idempotency_key)
        raise

    # Reaching this line means `complete_upload` returned rather than
    # raising, which only happens once the session is COMPLETED (either
    # just now, or on an earlier call this one safely replayed).
    response = APIResponse(
        message="Upload completed successfully.",
        data=UploadCompleteResponse(
            upload_id=upload_id,
            status=UploadSessionStatus.COMPLETED,
            actual_checksum=file.checksum,
            file=FileMetadataRead.model_validate(file),
        ),
    )

    if idempotency_key:
        await idempotency_service.complete(
            current_user.id, idempotency_key, fingerprint, status.HTTP_200_OK, response.model_dump(mode="json")
        )

    return response


@router.post(
    "/{upload_id}/cancel",
    response_model=APIResponse[UploadCancelResponse],
    summary="Cancel an in-progress upload (idempotent)",
)
async def cancel_upload(
    current_user: CurrentUser,
    upload_service: ChunkedUploadServiceDep,
    upload_id: uuid.UUID = Path(...),
) -> APIResponse[UploadCancelResponse]:
    session = await upload_service.cancel_upload(upload_id, current_user.id)
    return APIResponse(
        message="Upload cancelled.",
        data=UploadCancelResponse(upload_id=session.id, status=session.status, cancelled_at=session.cancelled_at),
    )


@router.delete(
    "/{upload_id}",
    response_model=APIResponse[None],
    summary="Cancel (if active) and permanently remove an upload session record",
)
async def delete_upload(
    current_user: CurrentUser,
    upload_service: ChunkedUploadServiceDep,
    upload_id: uuid.UUID = Path(...),
) -> APIResponse[None]:
    await upload_service.delete_upload(upload_id, current_user.id)
    return APIResponse(message="Upload session deleted.")
