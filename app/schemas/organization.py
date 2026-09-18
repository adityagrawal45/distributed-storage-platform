"""
Pydantic v2 schemas for Organization/Membership/Group endpoints (Phase 13).
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import MembershipStatus, OrganizationRole, OrganizationStatus


class OrganizationCreate(BaseModel):
    name: str = Field(examples=["Acme Corp"], min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name must not be empty.")
        return stripped


class OrganizationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: OrganizationStatus
    is_personal: bool
    storage_limit_bytes: int | None
    storage_used_bytes: int
    file_count: int
    max_members: int | None
    created_at: datetime


class OrganizationQuotaUpdate(BaseModel):
    storage_limit_bytes: int | None = Field(default=None, description="Null means unlimited.")


class MembershipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    role: OrganizationRole
    status: MembershipStatus
    created_at: datetime


class MemberAdd(BaseModel):
    email: str = Field(examples=["teammate@example.com"])
    role: OrganizationRole = OrganizationRole.MEMBER


class MemberRoleChange(BaseModel):
    role: OrganizationRole


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, examples=["Engineering"])

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name must not be empty.")
        return stripped


class GroupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    created_at: datetime


class GroupMemberAdd(BaseModel):
    user_id: uuid.UUID


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    result: str
    actor_user_id: uuid.UUID | None
    resource_type: str | None
    resource_id: uuid.UUID | None
    detail: dict | None
    created_at: datetime
