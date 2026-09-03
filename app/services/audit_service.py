"""
`AuditService` — the single gateway every security-sensitive code path
writes an audit event through (Phase 10).

Why one service instead of `await audit_repository.record(...)` at
every call site: the same reasoning `CacheService`'s module docstring
gives for centralizing Redis access — scattering raw writes means every
call site independently has to get right which fields are safe to
record and how to fail. In practice they wouldn't all get it right, and
this is a security control, so getting it wrong here is worse than
getting a cache write wrong.

The degradation contract
-------------------------
Unlike `CacheService` (where Redis is disposable) and unlike the Phase
8 outbox emitter (where a failure to *build* an event must not fail the
user's request), this class sits in an explicit tension the Phase 10
brief states directly: **"Security-critical events must not be
silently lost"** versus the rest of this codebase's own established
principle that an observability write must never fail the primary
operation it is observing.

The resolution taken here, consistent with how every other Phase in
this codebase resolves this exact tension: `record()` never raises —
a broken audit trail must not turn into a broken login — but a failure
to write is logged at ERROR with the full event context (event type,
actor, result), which is not "silent" in this codebase's own
established sense (Phase 7's `CacheService` and Phase 8's
`OutboxEmitterMixin` both use "logged loudly, never raised" as their
definition of "not lost" too). A stronger guarantee — buffering and
retrying a failed audit write, or blocking the request until the audit
row is durably committed to a *separate* system — would require
infrastructure (a dead-letter queue for audit failures, specifically)
this phase does not build; that gap is recorded honestly in
`docs/security/audit-logging.md` rather than papered over.

What must NEVER be passed to `record()`'s `detail` (or any other
field): a password (hashed or not), a JWT (access or refresh, in any
form), an API key, a private key, a full `Authorization` header, or a
signed URL. `detail` is a small JSONB blob for non-sensitive context
only (e.g. `{"reason": "refresh_token_reuse_detected"}`) — this is
enforced by convention at every call site in this codebase, not by
runtime filtering in this class, exactly the same trust boundary
`OutboxEmitterMixin`'s payload already relies on.
"""

from __future__ import annotations

import uuid

from app.core.enums import AuditEventType, AuditResult
from app.logging.logger import get_logger
from app.repositories.audit_log_repository import AuditLogRepository

logger = get_logger(__name__)


class AuditService:
    def __init__(self, repository: AuditLogRepository):
        self._repository = repository

    async def record(
        self,
        event_type: AuditEventType,
        *,
        result: AuditResult,
        actor_user_id: uuid.UUID | None = None,
        actor_email: str | None = None,
        resource_type: str | None = None,
        resource_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        request_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        try:
            await self._repository.record(
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
        except Exception as exc:  # noqa: BLE001 - see class docstring's degradation contract
            logger.error(
                "audit_log_write_failed",
                event_type=event_type.value,
                result=result.value,
                actor_user_id=str(actor_user_id) if actor_user_id else None,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id else None,
                error=str(exc),
            )
            return

        logger.info(
            "audit_event_recorded",
            event_type=event_type.value,
            result=result.value,
            actor_user_id=str(actor_user_id) if actor_user_id else None,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id else None,
        )
