"""Pydantic v2 schemas for secure sharing links (Phase 13 §14-17)."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.core.enums import Permission, ResourceType

if TYPE_CHECKING:
    from app.models.share import Share


class ShareCreate(BaseModel):
    resource_type: ResourceType
    resource_id: uuid.UUID
    permission: Permission = Permission.READ
    expires_in_hours: int | None = Field(default=None, description="Defaults to SHARE_DEFAULT_EXPIRATION_HOURS.")
    password: str | None = Field(default=None, min_length=1, max_length=255)
    max_downloads: int | None = Field(default=None, ge=1)


class ShareRead(BaseModel):
    """Never includes the raw token or `token_hash` — see `Share`'s model docstring."""

    id: uuid.UUID
    organization_id: uuid.UUID
    resource_type: ResourceType
    resource_id: uuid.UUID
    permission: Permission
    created_by: uuid.UUID
    has_password: bool
    expires_at: datetime | None
    revoked_at: datetime | None
    max_downloads: int | None
    download_count: int
    created_at: datetime

    @classmethod
    def from_share(cls, share: "Share") -> "ShareRead":
        """Explicit mapping, not `from_attributes` — `password_hash` (a bcrypt string) must never
        appear on the wire even as a truthy/falsy leak; this maps it to a plain boolean instead."""
        return cls(
            id=share.id,
            organization_id=share.organization_id,
            resource_type=share.resource_type,
            resource_id=share.resource_id,
            permission=share.permission,
            created_by=share.created_by,
            has_password=share.password_hash is not None,
            expires_at=share.expires_at,
            revoked_at=share.revoked_at,
            max_downloads=share.max_downloads,
            download_count=share.download_count,
            created_at=share.created_at,
        )


class ShareCreateResponse(BaseModel):
    """Returned exactly once, at creation — the ONLY response that ever carries the raw token."""

    share: ShareRead
    token: str
