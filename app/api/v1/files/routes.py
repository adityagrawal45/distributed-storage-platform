"""
File upload/download/replace/signed-URL routes (Phase 3).

Deliberately kept separate from `app/api/v1/metadata/routes.py`: metadata
routes (Phase 2) only ever touch `FileMetadataRepository` and never know
storage exists. Everything here that moves bytes goes through
`FileUploadService`, which is the only orchestrator that talks to both
the database and Google Cloud Storage — no GCS/StorageService import
belongs in this module, only calls to the service layer, per the
project's "no business logic in the API layer" rule.
"""

import uuid
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Header, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.core.config import get_settings
from app.dependencies.auth import CurrentUser
from app.dependencies.providers import FileUploadServiceDep, IdempotencyServiceDep
from app.exceptions.custom_exceptions import ValidationException
from app.schemas.file_metadata import FileMetadataRead, FileUploadResponse, SignedUrlResponse
from app.schemas.response import APIResponse
from app.services.idempotency_service import IdempotencyOutcome, compute_fingerprint

router = APIRouter(prefix="/files", tags=["File Storage"])


def _content_disposition(filename: str) -> str:
    """RFC 5987-encoded filename so non-ASCII names survive the header."""
    return f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"


def _parse_range_header(range_header: str, total_size: int) -> tuple[int, int]:
    """Parses a single-range `Range: bytes=start-end` header into an inclusive `(start, end)` pair."""
    try:
        units, _, range_spec = range_header.partition("=")
        if units.strip() != "bytes":
            raise ValueError
        start_str, _, end_str = range_spec.partition("-")

        if start_str == "":
            # "bytes=-500" -> last 500 bytes
            suffix_length = int(end_str)
            start = max(total_size - suffix_length, 0)
            end = total_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str else total_size - 1
    except (ValueError, TypeError) as exc:
        raise ValidationException("Malformed Range header.") from exc

    if start < 0 or end >= total_size or start > end:
        raise ValidationException("Requested Range is not satisfiable.")

    return start, end


@router.post(
    "/upload",
    response_model=APIResponse[FileUploadResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file's bytes to cloud storage and create its metadata",
    description=(
        "Supports an optional `Idempotency-Key` header (Phase 4): retrying the "
        "same upload with the same key after a network failure replays the "
        "original response instead of creating a duplicate file. See "
        "app/services/idempotency_service.py for the full contract."
    ),
)
async def upload_file(
    current_user: CurrentUser,
    file_upload_service: FileUploadServiceDep,
    idempotency_service: IdempotencyServiceDep,
    file: UploadFile = File(...),
    folder_id: uuid.UUID | None = Form(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> APIResponse[FileUploadResponse] | JSONResponse:
    if idempotency_key:
        fingerprint = compute_fingerprint(
            "upload", str(folder_id), file.filename or "", file.content_type or ""
        )
        check = await idempotency_service.check_or_begin(current_user.id, idempotency_key, fingerprint)
        if check.outcome is IdempotencyOutcome.REPLAY:
            # Safe retry: return exactly what the original attempt returned
            # instead of re-executing the (non-idempotent) upload.
            return JSONResponse(status_code=check.cached_status_code, content=check.cached_body)

    try:
        file_metadata, is_duplicate = await file_upload_service.upload_file(current_user.id, folder_id, file)
    except Exception:
        if idempotency_key:
            # Release the claim so a genuine failure (e.g. transient GCS
            # outage) can be retried immediately with the same key,
            # rather than blocking the client behind the full TTL.
            await idempotency_service.fail(current_user.id, idempotency_key)
        raise

    message = (
        "Identical content already existed; reused the existing storage object."
        if is_duplicate
        else "File uploaded successfully."
    )
    response = APIResponse(
        message=message,
        data=FileUploadResponse(file=FileMetadataRead.model_validate(file_metadata), is_duplicate=is_duplicate),
    )

    if idempotency_key:
        await idempotency_service.complete(
            current_user.id,
            idempotency_key,
            fingerprint,
            status.HTTP_201_CREATED,
            response.model_dump(mode="json"),
        )

    return response


@router.get("/{file_id}/download", summary="Stream a file's bytes, with HTTP Range support")
async def download_file(
    file_id: uuid.UUID,
    current_user: CurrentUser,
    file_upload_service: FileUploadServiceDep,
    request: Request,
):
    file = await file_upload_service.get_downloadable_file(file_id, current_user.id)
    media_type = file.mime_type or "application/octet-stream"
    disposition = _content_disposition(file.original_filename)

    range_header = request.headers.get("range")
    if range_header:
        start, end = _parse_range_header(range_header, file.size)
        chunk = await file_upload_service.download_range(file, start, end)
        return Response(
            content=chunk,
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file.size}",
                "Accept-Ranges": "bytes",
                "Content-Disposition": disposition,
                "Content-Length": str(end - start + 1),
            },
        )

    return StreamingResponse(
        file_upload_service.stream(file),
        media_type=media_type,
        headers={
            "Content-Disposition": disposition,
            "Accept-Ranges": "bytes",
            "Content-Length": str(file.size),
        },
    )


@router.get(
    "/{file_id}/signed-url",
    response_model=APIResponse[SignedUrlResponse],
    summary="Generate a time-boxed signed URL for direct, temporary access to a file's bytes",
)
async def get_signed_url(
    file_id: uuid.UUID,
    current_user: CurrentUser,
    file_upload_service: FileUploadServiceDep,
    expires_in_minutes: int | None = Query(
        default=None, ge=1, le=10080, description="Defaults to SIGNED_URL_EXPIRATION_MINUTES if omitted."
    ),
) -> APIResponse[SignedUrlResponse]:
    settings = get_settings()
    url = await file_upload_service.get_signed_url(file_id, current_user.id, expires_in_minutes)
    return APIResponse(
        message="Signed URL generated successfully.",
        data=SignedUrlResponse(url=url, expires_in_minutes=expires_in_minutes or settings.SIGNED_URL_EXPIRATION_MINUTES),
    )


@router.put(
    "/{file_id}/replace",
    response_model=APIResponse[FileMetadataRead],
    summary="Upload a new version of a file's bytes",
)
async def replace_file(
    file_id: uuid.UUID,
    current_user: CurrentUser,
    file_upload_service: FileUploadServiceDep,
    file: UploadFile = File(...),
) -> APIResponse[FileMetadataRead]:
    updated = await file_upload_service.replace_file(file_id, current_user.id, file, current_user.id)
    return APIResponse(message="File replaced successfully.", data=FileMetadataRead.model_validate(updated))


@router.delete(
    "/{file_id}/permanent",
    response_model=APIResponse[None],
    summary="Permanently delete a trashed file: removes its bytes from storage and its metadata row",
)
async def permanent_delete_file(
    file_id: uuid.UUID, current_user: CurrentUser, file_upload_service: FileUploadServiceDep
) -> APIResponse[None]:
    await file_upload_service.permanent_delete(file_id, current_user.id)
    return APIResponse(message="File permanently deleted from storage and metadata.")
