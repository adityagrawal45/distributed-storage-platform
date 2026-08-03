"""
Pydantic v2 schemas for File Metadata endpoints.

Design decision: filename validation reuses the same non-empty/no-path-
separators/length-limit rules as folder names (Phase 2 API boundary
principle) — invalid names never reach the service/repository layers.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.file_metadata import FileStatus, UploadStatus
from app.schemas.folder import _validate_folder_or_file_name


class FileMetadataCreate(BaseModel):
    original_filename: str = Field(examples=["report.pdf"])
    folder_id: uuid.UUID | None = Field(default=None, description="Omit or null for a top-level file.")
    stored_filename: str | None = Field(
        default=None, description="Overrides the auto-generated storage object key."
    )
    mime_type: str | None = Field(default=None, examples=["application/pdf"])
    size: int = Field(default=0, ge=0)
    checksum: str | None = Field(default=None, examples=["e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"])

    @field_validator("original_filename")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_folder_or_file_name(value)


class FileMetadataUpdate(BaseModel):
    mime_type: str | None = None
    size: int | None = Field(default=None, ge=0)
    checksum: str | None = None


class FileRename(BaseModel):
    name: str = Field(examples=["final-report.pdf"])

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_folder_or_file_name(value)


class FileMove(BaseModel):
    new_folder_id: uuid.UUID | None = Field(
        default=None, description="Target folder. Null moves the file to the top level."
    )


class FileMetadataRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    folder_id: uuid.UUID | None
    original_filename: str
    stored_filename: str
    extension: str | None
    mime_type: str | None
    size: int
    checksum: str | None
    version: int
    status: FileStatus
    storage_provider: str | None
    bucket_name: str | None
    object_name: str | None
    public_url: str | None
    storage_class: str | None
    etag: str | None
    upload_status: UploadStatus
    uploaded_at: datetime | None
    is_deleted: bool
    deleted_at: datetime | None
    created_by: uuid.UUID | None
    updated_by: uuid.UUID | None
    deleted_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class FileVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    file_id: uuid.UUID
    version: int
    checksum: str | None
    size: int
    created_at: datetime


class FileUploadResponse(BaseModel):
    """
    Wraps `FileMetadataRead` with an `is_duplicate` flag so clients can
    distinguish "your bytes were uploaded" from "identical content
    already existed and we reused it" without needing to diff two
    metadata payloads themselves (see FileUploadService docstring).
    """

    file: FileMetadataRead
    is_duplicate: bool = Field(
        default=False, description="True if identical content already existed; no new bytes were uploaded."
    )


class SignedUrlResponse(BaseModel):
    url: str
    expires_in_minutes: int
