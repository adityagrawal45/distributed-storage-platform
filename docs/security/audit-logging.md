# Audit Logging (Phase 10 — new)

Source of truth: `app/models/audit_log.py`, `app/repositories/audit_log_repository.py`,
`app/services/audit_service.py`, `app/core/enums.py::AuditEventType`/`AuditResult`,
`alembic/versions/0006_security_add_audit_log.py`.

## Why this is the centerpiece of Phase 10's changes

The repository inspection found every other Phase 10 checklist item
(RBAC, IDOR protection, rate limiting, password hashing, JWT
validation, Workload Identity, K8s hardening) **already correctly
implemented** in prior phases. Security audit logging was the one
genuine, total gap: no table, no model, no write path existed anywhere
for "who did what, when, with what result" as a reviewable,
query-able record. This is therefore the one area where Phase 10
built substantial new code rather than verifying and lightly extending
existing code.

## Design

- **Immutable by construction.** `AuditLogRepository` has no
  `update`/`delete` method — the same discipline `ReconciliationService`
  (Phase 9) established for its own read-only guarantee: enforced by
  what code exists, not by a runtime flag someone could flip.
- **No mixins.** Same precedent as `OutboxEvent`/`ProcessedEvent`
  (Phase 8): a row is written once and never touched again, so
  `AuditMixin`'s `updated_by`/`updated_at` would be meaningless.
- **`actor_user_id` is nullable.** A `LOGIN_FAILURE` against an email
  with no matching user has no user to attribute it to —
  `actor_email` (captured independently) is what still makes failed-
  login auditing useful for abuse investigation in that case.
- **Generic `resource_type`/`resource_id`, not a per-resource FK.** One
  shared audit trail spans files, users, and (in the future) whatever
  else gets audited — a real FK would have to pick one table.

## The degradation-contract tension, and how it was resolved

The Phase 10 brief states two things that are, on their face, in
tension:

> "Security-critical events must not be silently lost."

versus this codebase's own established, repeated principle (Phase 7's
`CacheService`, Phase 8's `OutboxEmitterMixin`) that an observability
write must never fail the primary operation it is observing.

**Resolution taken** (`AuditService.record`, see its own docstring for
the full reasoning): the write never raises — a broken audit trail
must not turn a login into a 500 — but a failed write is logged at
**ERROR** with full event context (event type, actor, result), which
is this codebase's own established definition of "not silently lost."
A stronger guarantee (a dead-letter queue for failed audit writes, or
blocking the request on a durable write to a *separate* system) would
require infrastructure this phase does not build.

**Honest gap**: if Postgres itself is down, an audit write fails and
is only ever recorded in application logs (which may or may not be
shipped to a durable, separate log sink depending on deployment) — it
is NOT written to a secondary durable store. For a stricter compliance
posture (e.g. an environment where "the audit trail commits or the
operation fails" is a hard requirement), this would need to change;
it was a deliberate scope decision for this pass, not an oversight.

## What is recorded (`AuditEventType`)

`LOGIN_SUCCESS`, `LOGIN_FAILURE`, `LOGOUT`, `TOKEN_REFRESH`,
`TOKEN_REVOCATION` (reuse-detected), `FILE_DOWNLOAD` (direct + signed
URL, distinguished by `detail.method`), `FILE_DELETE` (permanent
delete only — trash/soft-delete is reversible and not audited),
`ADMIN_ACTION` (currently: an admin viewing another user's profile).

**Deliberately NOT emitted this phase** (the Phase 10 brief's example
list is illustrative, not a mandatory minimum — see the brief's own
final rule against unnecessary code): `UPLOAD_START`/`UPLOAD_COMPLETE`
(would require touching `ChunkedUploadService`, the most complex file
in the codebase, for a lower-severity event class than delete/download);
`PASSWORD_CHANGE`/`PASSWORD_RESET` (no such feature exists — see
`authentication.md`); `PERMISSION_CHANGE` (no API path changes a
user's role today — `authorization.md`); `SUSPICIOUS_REQUEST` (no
general-purpose anomaly detector exists to emit it from; the one
concrete suspicious-request case this phase found — refresh-token
reuse — IS covered, as `TOKEN_REVOCATION`).

## What is NEVER recorded

Enforced by convention at every call site (there is no runtime filter
in `AuditService` — the same trust boundary `OutboxEmitterMixin`'s
payload already relies on): passwords (hashed or not), JWTs (access or
refresh, in any form), API keys, private keys, full `Authorization`
headers, signed URLs. Verified explicitly in
`tests/test_security_phase10.py::test_login_failure_wrong_password_writes_an_audit_row_with_no_secret_leaked`
and `::test_signed_url_writes_a_file_download_audit_row_without_logging_the_url`.

## Where each event is recorded, and why there

Two different layers record audit events, deliberately:

- **Inside the service** (`AuthService`, `FileUploadService`): for
  mutations where the relevant IDs are already in hand from the
  operation itself (login/logout/refresh/file-delete). Same DI
  pattern Phase 7/8 established for `cache=`/`invalidator=`/`outbox=`
  — `audit: AuditService | None = None`, keyword-only, defaulting to
  `None` so every pre-existing test construction of these services
  keeps working unchanged.
- **At the route layer** (`files/routes.py`, `users/routes.py`): for
  read/access events (download, signed URL, admin lookup) where the
  client IP (`request.state.client_ip`, resolved by
  `TrustedProxyMiddleware`) is naturally available and there is no
  mutation/transaction to piggyback the write onto.

## Not yet built

- No `GET /users/me/security-log` self-service endpoint — the
  `list_for_user`/`list_by_event_type` repository methods exist and are
  tested indirectly (via direct DB queries in
  `tests/test_security_phase10.py`) but are not wired to a route.
- No export/SIEM-shipping pipeline — rows live in Postgres only.
- No retention policy — rows accumulate indefinitely (`audit_logs` has
  no TTL/archival job). For a real production deployment this needs a
  decision (compliance requirements typically mandate a MINIMUM
  retention, not a maximum, so this is lower urgency than it might
  first appear, but it is unaddressed).
