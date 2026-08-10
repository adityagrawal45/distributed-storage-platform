"""
Pydantic v2 schemas for chunked/resumable upload endpoints (Phase 6).

Design decision: `uploaded_chunks`/`missing_chunks` on `UploadProgressRead`
are flat lists of chunk numbers (not range-compressed pairs like
`[[1,50]]`) — simpler for clients to consume, and bounded in size by
`Settings.MAX_CHUNKS_PER_UPLOAD` (10000 by default), which keeps the
JSON payload small even at that ceiling.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import ChunkStatus, UploadSessionStatus
from app.schemas.file_metadata import FileMetadataRead
from app.schemas.folder import _validate_folder_or_file_name


class UploadInitiateRequest(BaseModel):
    filename: str = Field(examples=["large-video.mp4"])
    size: int = Field(gt=0, examples=[10737418240], description="Total file size in bytes.")
    mime_type: str | None = Field(default=None, examples=["video/mp4"])
    folder_id: uuid.UUID | None = Field(default=None, description="Omit or null for a top-level file.")
    chunk_size: int | None = Field(
        default=None,
        gt=0,
        examples=[104857600],
        description="Bytes per chunk. Omit to use the server default (Settings.CHUNK_DEFAULT_SIZE_BYTES).",
    )
    checksum: str | None = Field(
        default=None,
        description="Optional expected SHA-256 of the whole file, verified at completion.",
        examples=["e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"],
    )

    @field_validator("filename")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_folder_or_file_name(value)


class UploadInitiateResponse(BaseModel):
    upload_id: uuid.UUID
    chunk_size: int
    total_chunks: int
    total_size: int
    expires_at: datetime
    status: UploadSessionStatus


class UploadProgressRead(BaseModel):
    upload_id: uuid.UUID
    status: UploadSessionStatus
    filename: str
    total_size: int
    chunk_size: int
    total_chunks: int
    uploaded_chunks: list[int]
    missing_chunks: list[int]
    uploaded_bytes: int
    progress_percentage: float
    expires_at: datetime
    created_at: datetime


class ChunkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    chunk_number: int
    size: int
    checksum: str | None
    status: ChunkStatus
    uploaded_at: datetime | None


class ChunkUploadResponse(BaseModel):
    upload_id: uuid.UUID
    chunk_number: int
    status: ChunkStatus
    size: int
    checksum: str | None
    uploaded_bytes: int
    total_size: int
    progress_percentage: float


class UploadCompleteResponse(BaseModel):
    upload_id: uuid.UUID
    status: UploadSessionStatus
    actual_checksum: str | None
    file: FileMetadataRead


class UploadCancelResponse(BaseModel):
    upload_id: uuid.UUID
    status: UploadSessionStatus
    cancelled_at: datetime | None
