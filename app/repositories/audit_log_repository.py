"""
Audit log repository (Phase 10).

Deliberately WRITE-ONLY beyond simple reads: there is no `update`/
`delete` method anywhere in this class, by construction — the same
"immutable by construction, not by a flag" discipline
`ReconciliationService` established in Phase 9 for its own read-only
guarantee. An audit trail with a mutation path is not an audit trail.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AuditEventType, AuditResult
from app.models.audit_log import AuditLog
from app.repositories.base import BaseRepository


class AuditLogRepository(BaseRepository[AuditLog]):
    model = AuditLog

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def record(
        self,
        *,
        event_type: AuditEventType,
        result: AuditResult,
        actor_user_id: uuid.UUID | None = None,
        actor_email: str | None = None,
        organization_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        request_id: str | None = None,
        detail: dict | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            event_type=event_type,
            result=result,
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            organization_id=organization_id,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip_address,
            request_id=request_id,
            detail=detail,
        )
        return await self.add(entry)

    async def search(
        self,
        *,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: uuid.UUID | None = None,
        event_type: AuditEventType | None = None,
        result: AuditResult | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[AuditLog], int]:
        """
        Bounded, filterable, paginated search (Phase 13 §27) —
        `organization_id` is REQUIRED, not optional, so this method
        cannot be called in a way that returns another tenant's events;
        there is deliberately no "search across all organizations"
        variant here (a platform-wide audit view, if ever needed, is a
        separate, explicitly-platform-scoped capability, not a missing
        `organization_id is None` branch on this one).
        """
        conditions = [AuditLog.organization_id == organization_id]
        if actor_user_id is not None:
            conditions.append(AuditLog.actor_user_id == actor_user_id)
        if resource_type is not None:
            conditions.append(AuditLog.resource_type == resource_type)
        if resource_id is not None:
            conditions.append(AuditLog.resource_id == resource_id)
        if event_type is not None:
            conditions.append(AuditLog.event_type == event_type)
        if result is not None:
            conditions.append(AuditLog.result == result)
        if after is not None:
            conditions.append(AuditLog.created_at >= after)
        if before is not None:
            conditions.append(AuditLog.created_at <= before)

        count_result = await self._session.execute(
            select(func.count()).select_from(AuditLog).where(*conditions)
        )
        total = count_result.scalar_one()

        result_rows = await self._session.execute(
            select(AuditLog).where(*conditions).order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
        )
        return list(result_rows.scalars().all()), total

    async def list_for_user(self, actor_user_id: uuid.UUID, *, limit: int = 100) -> list[AuditLog]:
        """
        Most-recent-first page of a user's own audit trail. Not wired to
        an API endpoint in this phase (no self-service "my security
        activity" route exists yet) — provided so the security test
        suite can assert against what was actually written without
        reaching into the ORM directly, and as the seam a future
        `GET /users/me/security-log` endpoint would use.
        """
        result = await self._session.execute(
            select(AuditLog)
            .where(AuditLog.actor_user_id == actor_user_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_by_event_type(
        self, event_type: AuditEventType, *, after: datetime | None = None, limit: int = 100
    ) -> list[AuditLog]:
        """Used by the security test suite and by any future SIEM/export job."""
        conditions = [AuditLog.event_type == event_type]
        if after is not None:
            conditions.append(AuditLog.created_at >= after)
        result = await self._session.execute(
            select(AuditLog).where(*conditions).order_by(AuditLog.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())
