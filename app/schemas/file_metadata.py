"""
Pydantic v2 schemas for Folder endpoints.

Design decision: folder name validation (non-empty, no path separators,
length limits, no leading/trailing whitespace) is enforced HERE, at the
API boundary — the same principle used for password strength in Phase 1
— so invalid names never reach the service/repository layers.
"""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_INVALID_NAME_CHARS = re.compile(r'[/\\:*?"<>|]')


def _validate_folder_or_file_name(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("Name must not be empty.")
    if len(stripped) > 255:
        raise ValueError("Name must not exceed 255 characters.")
    if _INVALID_NAME_CHARS.search(stripped):
        raise ValueError('Name must not contain any of: / \\ : * ? " < > |')
    if stripped in {".", ".."}:
        raise ValueError("Name must not be '.' or '..'.")
    return stripped


class FolderCreate(BaseModel):
    name: str = Field(examples=["Projects"])
    parent_folder_id: uuid.UUID | None = Field(
        default=None, description="Omit or null to create a top-level folder."
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_folder_or_file_name(value)


class FolderRename(BaseModel):
    name: str = Field(examples=["Projects (Archived)"])

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_folder_or_file_name(value)


class FolderMove(BaseModel):
    new_parent_folder_id: uuid.UUID | None = Field(
        default=None, description="Target parent folder. Null moves the folder to the top level."
    )


class FolderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    parent_folder_id: uuid.UUID | None
    name: str
    path: str
    level: int
    is_root: bool
    is_deleted: bool
    deleted_at: datetime | None
    created_by: uuid.UUID | None
    updated_by: uuid.UUID | None
    deleted_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class FolderTreeNode(BaseModel):
    """Recursive tree representation used by GET /folders/tree."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    path: str
    level: int
    children: list["FolderTreeNode"] = Field(default_factory=list)


FolderTreeNode.model_rebuild()


class BreadcrumbItem(BaseModel):
    id: uuid.UUID
    name: str
    path: str


class BreadcrumbResponse(BaseModel):
    items: list[BreadcrumbItem]