# Phase 10 — Final Security Report

## Executive Summary

A full repository inspection (per the Phase 10 brief's mandatory-
inspection rule) found NimbusFS's existing security posture, built
incrementally across Phases 1–9, to be **substantially already
correct**: working RBAC, IDOR protection baked into every repository
query rather than bolted onto route handlers, bcrypt password hashing
with strength validation, JWT with type/issuer/expiry validation and
rotation-based refresh-token revocation, distributed Redis-backed rate
limiting, least-privilege GCP IAM via 6 separate Workload-Identity-bound
service accounts, hardened non-root Kubernetes Pods, a private GCS
bucket with expiring signed URLs, and no hardcoded secrets. Phase 10
therefore did **not** rebuild any of that — it inspected it, verified
it against the actual files (not the phase narrative), and made six
concrete, targeted changes where a real gap was found:

1. Built a new **security audit trail** (`AuditLog`) — the one genuine
   total gap found — covering login/logout/refresh/token-revocation/
   file-delete/file-download/admin-action events.
2. Hardened refresh-token **reuse detection**: replaying an
   already-rotated token now revokes the user's entire session family
   and is recorded, instead of only being rejected.
3. Closed a rate-limiting gap: **`/auth/refresh` was unmetered**
   despite being reachable without prior authentication, the same
   abuse shape as `/login`/`/register`.
4. Patched **two dependencies with known CVEs directly on the security
   attack surface**: `python-jose` (the JWT library) and
   `python-multipart` (the untrusted-upload parser).
5. Added a **security test suite** (`tests/test_security_phase10.py`,
   13 tests) covering all of the above plus explicit cross-user IDOR
   checks on the two newly-audited operations.
6. Wrote this documentation set (`docs/security/`).

**No breaking changes.** Every one of the 416 pre-existing tests still
passes unmodified; the 13 new tests bring the suite to **429 passing,
0 failing**.

## Existing Security Posture (as found, before this phase)

See `authentication.md`, `authorization.md`, and `infrastructure.md`
for the full per-item inspection tables. Summary: JWT ✅, password
hashing ✅, RBAC ✅, IDOR/resource-level authorization ✅, rate limiting
✅ (with one gap — see below), GCS/signed-URL security ✅, GCP IAM ✅,
Kubernetes hardening ✅, container security ✅, no hardcoded secrets ✅.

## Security Improvements (this phase)

| # | Improvement | File(s) |
|---|---|---|
| 1 | `AuditLog` model, repository, service, migration | `app/models/audit_log.py`, `app/repositories/audit_log_repository.py`, `app/services/audit_service.py`, `alembic/versions/0006_security_add_audit_log.py` |
| 2 | Audit wiring: login success/failure, logout, refresh, token revocation | `app/services/auth_service.py` |
| 3 | Audit wiring: file delete | `app/services/file_upload_service.py` |
| 4 | Audit wiring: file download (direct + signed URL) | `app/api/v1/files/routes.py` |
| 5 | Audit wiring: admin action | `app/api/v1/users/routes.py` |
| 6 | Refresh-token reuse → whole-session-family revocation | `app/repositories/refresh_token_repository.py::revoke_all_for_user`, `app/services/auth_service.py::refresh` |
| 7 | `/auth/refresh` rate limiting | `app/core/rate_limiter.py`, `app/core/config/settings.py`, `app/api/v1/auth/routes.py` |
| 8 | Dependency CVE fixes | `requirements.txt` (`python-jose` 3.3.0→3.5.0, `python-multipart` 0.0.20→0.0.32) |
| 9 | DI wiring for the above | `app/dependencies/providers.py` |

## Vulnerabilities Found

| Finding | Severity (contextual) | Fixed? |
|---|---|---|
| No security audit trail existed | Medium (no direct exploit; blocks incident investigation/compliance) | ✅ Fixed |
| Refresh-token replay was rejected but not reacted to | Low-Medium (an attacker with ONE stolen, already-rotated token was blocked, but a stolen CURRENT token's sibling sessions were not proactively revoked) | ✅ Fixed |
| `/auth/refresh` unmetered | Low-Medium (abuse/DoS vector, same class as login/register which WERE metered) | ✅ Fixed |
| `python-jose` 3.3.0 — known CVEs (algorithm-confusion/DoS class) | Medium-High (directly on the JWT verification path) | ✅ Fixed |
| `python-multipart` 0.0.20 — 6 known CVEs | Medium (untrusted-input parser) | ✅ Fixed |
| `JWT_SECRET_KEY` dev-default has no production-startup guard | Low (operational hygiene, not a code defect — see `authentication.md`) | ❌ Not fixed — recorded as remaining risk |
| `ALLOWED_HOSTS` defaults to `*`, no production-startup guard | Low (same class as above) | ❌ Not fixed — recorded as remaining risk |
| `starlette`/transitively `fastapi` — 9 known CVEs | Unassessed severity (transitive; would require a framework-version upgrade pass) | ❌ Not fixed — recorded as remaining risk |
| `ecdsa` — 1 known CVE, no fix available | Low under current config (HS256 default; ECDSA path unused) | ❌ Not fixable by version bump — accepted, monitored |
| No account-lockout beyond rate limiting | Low (rate limiting already provides a meaningful control) | Not built — documented design choice, not a gap this phase closes |
| No password-reset feature exists at all | N/A (not a vulnerability — a feature that doesn't exist can't be insecure) | Not built — see `authentication.md` |

## Vulnerabilities Fixed

See the "Vulnerabilities Found" table's ✅ rows — five findings fixed:
audit-trail gap, reuse-detection gap, refresh rate-limit gap, and two
dependency CVEs.

## Remaining Risks

1. **Production secret/host defaults have no startup guard.**
   `JWT_SECRET_KEY` and `ALLOWED_HOSTS` both have permissive dev
   defaults with no code path that fails startup if
   `ENVIRONMENT=production` and either is still at its default. Cheap
   to close in a follow-up (`if settings.is_production and
   settings.JWT_SECRET_KEY == <default>: raise RuntimeError(...)` in
   `app/main.py`'s startup sequence) — not implemented this phase to
   keep the change surface matched to what was actually audited.
2. **`starlette`/`fastapi` carry known CVEs** at their currently pinned
   versions. Not bumped this phase — see `dependency-audit.md` for
   why (framework-level upgrade, real regression-risk surface beyond
   what this phase's testing could fully re-verify).
3. **`ecdsa`'s Minerva-class timing side-channel has no published
   fix.** Not exploitable under NimbusFS's default HS256 configuration;
   would matter only if `JWT_ALGORITHM` were changed to an EC-based
   algorithm.
4. **No Secret Manager integration** — Kubernetes Secrets only, base64
   not KMS-encrypted by default. `infrastructure.md`'s own assessment.
5. **Audit trail has no export/retention policy** and is not itself
   backed by a secondary durable store if Postgres is down at write
   time (though failures ARE logged loudly, never silent).
6. **No device/session-tracking table** beyond the `RefreshToken`
   row's existing `user_id`/`created_at`/`expires_at`/`revoked` fields
   — no device metadata, no self-service "view/revoke my sessions" UI.
7. **No account lockout** beyond rate limiting.

## Authentication / Authorization / RBAC / User Isolation Architecture

Fully documented in `authentication.md` and `authorization.md` — not
repeated here to avoid duplication (per the brief's own "do not
duplicate existing documentation" instruction).

## GCP IAM / Kubernetes Security / Secrets Findings

Fully documented in `infrastructure.md`.

## Dependency Findings

Fully documented in `dependency-audit.md`.

## Security Tests

`tests/test_security_phase10.py` — 13 tests, all passing:
audit-row assertions for `LOGIN_SUCCESS`/`LOGIN_FAILURE` (both known-
and unknown-email cases)/`LOGOUT`/`TOKEN_REFRESH`/`TOKEN_REVOCATION`/
`FILE_DELETE`/`FILE_DOWNLOAD` (both direct and signed-URL paths)/
`ADMIN_ACTION`; the full refresh-token-reuse-revokes-the-whole-family
scenario across 3 sessions; the `/auth/refresh` rate-limit contract;
and two explicit cross-user IDOR checks (signed URL, permanent
delete) on the newly-audited operations. Pre-existing IDOR coverage in
`test_file_storage.py`/`test_folders.py` was NOT duplicated.

**Full suite result: 429 passed, 0 failed** (416 pre-existing + 13
new), confirmed by running `pytest -q` twice — once immediately after
the code changes, once again after the two dependency bumps were
actually installed into the test environment.

## Threat Model Summary

Full STRIDE-structured model in `threat-model.md`. Every threat
category has at least one item Phase 10 either newly mitigates
(spoofing via stolen refresh token; repudiation via the audit trail;
DoS via unmetered refresh) or confirms was already mitigated by prior
phases.

## Breaking Changes

**None.** Every service constructor change (`AuthService`,
`FileUploadService` gaining an `audit` parameter) follows the exact
keyword-only-defaulting-to-`None` pattern Phase 7/8 established for
`cache=`/`invalidator=`/`outbox=` — every pre-existing construction
(including all 416 pre-existing tests) works unchanged.
`AuthService.login`/`refresh`/`logout` gaining an `ip_address`
keyword-only parameter is the same shape. The two route-handler
signature changes (`request: Request` added to `login`/`refresh`/
`logout`/`get_signed_url`/`get_user_by_id`) are additive — FastAPI
injects `Request` for free, no client-visible contract changed. The
one new HTTP-behavior change a client could observe is that
`/auth/refresh` can now return 429 under heavy use — a REFRESH budget
of 20 requests/60s (a reasonable multiple of the 15-minute access-
token lifetime) was chosen so no realistic legitimate client pattern
crosses it.

## Migration Requirements

Run `alembic upgrade head` to create the `audit_logs` table (and its
two new Postgres enum types) before deploying this phase's code — the
migration has never been run against a real Postgres in this session
(same honest caveat every prior migration in this codebase carries;
verified only via the SQLite-backed test suite and import-correctness).
No data backfill is needed or possible (audit history before this
phase's deployment does not exist to backfill). No other schema
changes.

## Production Security Recommendations

In priority order:

1. Add the startup guard for `JWT_SECRET_KEY`/`ALLOWED_HOSTS`
   production defaults (cheap, closes a real gap).
2. Plan a dedicated FastAPI/Starlette upgrade pass to close the 9
   `starlette` CVEs — separate from this phase, with its own
   regression-test budget.
3. Migrate secrets from Kubernetes Secrets to Google Secret Manager +
   the CSI driver.
4. Run a real Gitleaks/Trivy/Semgrep/Bandit scan (none were available
   in this session's environment) before a production release.
5. Wire `pip-audit` (or equivalent) into CI once CI exists, so
   dependency CVEs are caught continuously rather than by point-in-time
   audit.
6. Decide and implement an audit-log retention/export policy before
   the table grows unbounded in a real deployment.
7. Consider account lockout as a defense-in-depth layer beyond rate
   limiting, if the product/compliance requirements call for it.

## Implementation Status (per §30 of the Phase 10 brief)

| Feature | Status |
|---|---|
| Audit logging system | IMPLEMENTED, TESTED (13 tests, SQLite-backed) — **not MEASURED** against real Postgres (migration never run against one) |
| Refresh-token reuse → session-family revocation | IMPLEMENTED, TESTED |
| `/auth/refresh` rate limiting | IMPLEMENTED, TESTED |
| Dependency CVE fixes | IMPLEMENTED, TESTED (full suite re-run against patched versions) |
| RBAC | Pre-existing, IMPLEMENTED, TESTED (inherited coverage + this phase's admin-action test) |
| IDOR/resource-level authorization | Pre-existing, IMPLEMENTED, TESTED |
| JWT security | Pre-existing, IMPLEMENTED, TESTED; this phase's dependency bump is IMPLEMENTED, TESTED |
| GCP IAM / Workload Identity | DESIGNED, IMPLEMENTED (Terraform) — **NOT MEASURED**, no real GCP project applied this Terraform |
| Kubernetes hardening | DESIGNED, IMPLEMENTED (manifests) — **NOT MEASURED**, no real cluster |
| Secret Manager integration | **DESIGNED only** — not implemented |
| Startup guards for prod-secret defaults | **Not designed, not implemented** — recorded as a recommendation only |

**Explicitly not claimed**: "production ready," "secure" (unqualified),
"HA," or "resilient" for anything in this table marked DESIGNED/
IMPLEMENTED without also being MEASURED — consistent with the Phase 10
brief's own rule against overclaiming.
