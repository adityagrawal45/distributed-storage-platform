"""
`ShareService` — time-boxed, revocable, optionally-password-protected
bearer links (Phase 13 §14-17).

Token design (Phase 13 §15-16)
--------------------------------
`secrets.token_urlsafe(settings.SHARE_TOKEN_BYTES)` — `secrets`, not
`random`, because this is a genuine bearer credential (see
`app/models/share.py`'s docstring for the "store a hash, not the
secret" half of this). 32 bytes (256 bits) of CSPRNG entropy,
URL-safe-base64-encoded, is astronomically infeasible to guess or
enumerate — nobody is brute-forcing a 256-bit space one HTTP request
at a time, which is what makes SHA-256 (fast, not deliberately slow)
the correct hash here: the token's OWN entropy is the security
property, not the hash function's cost (contrast `password_hash`,
which protects a human-chosen, comparatively low-entropy secret and
therefore DOES need bcrypt's deliberate slowness).

Redemption never reveals WHY access failed (Phase 13 §16/§36):
non-existent, expired, and revoked tokens all raise the identical
`ShareExpiredOrRevokedException` (404) — see that exception's own
docstring. A WRONG PASSWORD on an otherwise-valid share is the one
case that gets its own distinct exception (`InvalidSharePasswordException`,
403) — the share's existence is already unavoidably confirmed to the
caller at that point (they were prompted for a password), so there is
no oracle left to protect by also hiding this one.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.enums import AuditEventType, AuditResult, Permission, ResourceType
from app.core.security.password import hash_password, verify_password
from app.exceptions.custom_exceptions import (
    InvalidSharePasswordException,
    ShareExpiredOrRevokedException,
    ShareNotFoundException,
    ValidationException,
)
from app.logging.logger import get_logger
from app.models.share import Share
from app.repositories.share_repository import ShareRepository
from app.services.audit_service import AuditService

logger = get_logger(__name__)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ShareService:
    def __init__(self, share_repository: ShareRepository, *, audit: AuditService | None = None):
        self._shares = share_repository
        self._audit = audit

    async def create(
        self,
        *,
        actor_user_id: uuid.UUID,
        organization_id: uuid.UUID,
        resource_type: ResourceType,
        resource_id: uuid.UUID,
        permission: Permission,
        expires_in_hours: int | None,
        password: str | None,
        max_downloads: int | None,
    ) -> tuple[Share, str]:
        """
        Returns `(share_row, raw_token)` — the raw token is returned
        to the CALLER exactly once, here, and is never retrievable
        again (`Share.token_hash` cannot be reversed) — this method is
        the only place in the entire codebase that ever sees the plain
        token, mirroring `AuthService`'s access/refresh token issuance.
        """
        settings = get_settings()
        hours = expires_in_hours if expires_in_hours is not None else settings.SHARE_DEFAULT_EXPIRATION_HOURS
        if hours <= 0 or hours > settings.SHARE_MAX_EXPIRATION_HOURS:
            raise ValidationException(
                f"expires_in_hours must be between 1 and {settings.SHARE_MAX_EXPIRATION_HOURS}."
            )

        raw_token = secrets.token_urlsafe(settings.SHARE_TOKEN_BYTES)
        share = Share(
            organization_id=organization_id,
            resource_type=resource_type,
            resource_id=resource_id,
            token_hash=_hash_token(raw_token),
            permission=permission,
            created_by=actor_user_id,
            password_hash=hash_password(password) if password else None,
            expires_at=datetime.now(UTC) + timedelta(hours=hours),
            max_downloads=max_downloads,
        )
        share = await self._shares.add(share)

        if self._audit is not None:
            await self._audit.record(
                AuditEventType.SHARE_CREATED,
                result=AuditResult.SUCCESS,
                actor_user_id=actor_user_id,
                organization_id=organization_id,
                resource_type=resource_type.value,
                resource_id=resource_id,
                detail={"permission": permission.value, "expires_in_hours": hours, "password_protected": bool(password)},
                # Never the raw token, never a substring of it — see module docstring.
            )
        logger.info(
            "share_created",
            share_id=str(share.id),
            organization_id=str(organization_id),
            resource_type=resource_type.value,
            resource_id=str(resource_id),
        )
        return share, raw_token

    async def get_owned(self, share_id: uuid.UUID, organization_id: uuid.UUID) -> Share:
        share = await self._shares.get_owned(share_id, organization_id)
        if share is None:
            raise ShareNotFoundException()
        return share

    async def list_for_organization(
        self, organization_id: uuid.UUID, *, created_by: uuid.UUID | None, limit: int, offset: int
    ) -> tuple[list[Share], int]:
        return await self._shares.list_for_organization(organization_id, created_by=created_by, limit=limit, offset=offset)

    async def revoke(self, actor_user_id: uuid.UUID, organization_id: uuid.UUID, share_id: uuid.UUID) -> None:
        share = await self.get_owned(share_id, organization_id)
        if share.revoked_at is None:
            await self._shares.revoke(share)
            if self._audit is not None:
                await self._audit.record(
                    AuditEventType.SHARE_REVOKED,
                    result=AuditResult.SUCCESS,
                    actor_user_id=actor_user_id,
                    organization_id=organization_id,
                    resource_type=share.resource_type.value,
                    resource_id=share.resource_id,
                )
        logger.info("share_revoked", share_id=str(share_id), organization_id=str(organization_id))

    async def redeem(self, raw_token: str, *, password: str | None) -> Share:
        """
        The public, UNAUTHENTICATED entry point — see this module's
        docstring for why every failure mode short of a wrong password
        collapses into one identical 404. Also enforces expiry,
        revocation, and `max_downloads` — all evaluated fresh on every
        redemption (never cached; PostgreSQL via this repository call
        is the sole source of truth for share validity, per Phase 13
        §18's "cache is only ever an optimization" — this path has no
        cache in front of it at all, precisely to avoid that class of
        bug entirely rather than needing to invalidate it correctly).
        """
        share = await self._shares.get_by_token_hash(_hash_token(raw_token))
        if share is None:
            raise ShareExpiredOrRevokedException()
        if share.revoked_at is not None:
            raise ShareExpiredOrRevokedException()
        expires_at = share.expires_at
        if expires_at is not None:
            # SQLite (test backing store only) returns a naive datetime
            # for a `DateTime(timezone=True)` column even though it was
            # written tz-aware — same accommodation `ChunkedUploadService`
            # already makes for `UploadSession.expires_at`.
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= datetime.now(UTC):
                raise ShareExpiredOrRevokedException()
        if share.max_downloads is not None and share.download_count >= share.max_downloads:
            raise ShareExpiredOrRevokedException()

        if share.password_hash is not None and (not password or not verify_password(password, share.password_hash)):
            raise InvalidSharePasswordException()

        return share

    async def record_access(self, share: Share) -> None:
        await self._shares.increment_download_count(share.id)
        if self._audit is not None:
            await self._audit.record(
                AuditEventType.SHARE_ACCESSED,
                result=AuditResult.SUCCESS,
                organization_id=share.organization_id,
                resource_type=share.resource_type.value,
                resource_id=share.resource_id,
                detail={"share_id": str(share.id)},
            )
