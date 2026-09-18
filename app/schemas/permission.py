"""Pydantic v2 schemas for resource permission grants (Phase 13 §11-13)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.core.enums import Permission, PrincipalType, ResourceType


class PermissionGrantCreate(BaseModel):
    resource_type: ResourceType
    resource_id: uuid.UUID
    principal_type: PrincipalType
    principal_id: uuid.UUID
    permission: Permission


class PermissionRevoke(BaseModel):
    resource_type: ResourceType
    resource_id: uuid.UUID
    principal_type: PrincipalType
    principal_id: uuid.UUID
    permission: Permission | None = None


class ResourcePermissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    resource_type: ResourceType
    resource_id: uuid.UUID
    principal_type: PrincipalType
    principal_id: uuid.UUID
    permission: Permission
    granted_by: uuid.UUID | None
    created_at: datetime
