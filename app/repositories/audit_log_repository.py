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

from sqlalchemy import select
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
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip_address,
            request_id=request_id,
            detail=detail,
        )
        return await self.add(entry)

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
