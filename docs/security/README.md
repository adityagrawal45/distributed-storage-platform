# NimbusFS Security Documentation (Phase 10)

This directory documents NimbusFS's security architecture as it
actually exists in the codebase — inspected, audited, and (where a
real gap was found) hardened in Phase 10. It supplements, and does not
duplicate, the architecture already documented in the main `README.md`
and `CONTEXT.md`.

**Read `final-report.md` first** — it is the executive summary and the
acceptance-criteria checklist for this phase. The other files here are
the supporting depth behind each of its sections.

| File | Covers |
|---|---|
| `final-report.md` | Executive summary, findings, fixes, remaining risks, implementation-status classification (Section 29/30 of the Phase 10 brief) |
| `authentication.md` | JWT, password hashing, refresh-token rotation + the new reuse-detection hardening, session model |
| `authorization.md` | RBAC, resource-level (IDOR) authorization, user data isolation |
| `audit-logging.md` | The new `AuditLog` system — design, degradation contract, what is/isn't recorded |
| `infrastructure.md` | GCP IAM, Kubernetes hardening, secrets management, network security, GCS/signed-URL security — mostly a pointer into the Phase 5/8/9/Terraform work already done, with this phase's own inspection notes |
| `dependency-audit.md` | The `pip-audit` scan, findings, and the two dependency bumps this phase applied |
| `threat-model.md` | STRIDE-structured threat model for the assets Phase 10 was scoped to protect |

## What Phase 10 actually changed

A short version — see `final-report.md` for the full accounting:

- **New**: `AuditLog` model/repository/service, wired into login
  success/failure, logout, token refresh, token revocation, file
  delete, file download (direct + signed URL), and admin actions on
  another user's profile.
- **New**: refresh-token reuse (replay of an already-rotated token)
  now revokes the user's ENTIRE session family and is recorded as a
  `TOKEN_REVOCATION` audit event, instead of only being rejected.
- **New**: `/auth/refresh` is now rate-limited (`RateLimitCategory.REFRESH`)
  — it was previously unmetered despite being reachable without prior
  authentication, the same abuse shape as `/login`/`/register`.
- **Fixed**: two dependencies with known CVEs directly on the security
  attack surface (`python-jose`, the JWT library; `python-multipart`,
  the untrusted-upload parser) bumped to patched versions — see
  `dependency-audit.md`.
- **Everything else** (RBAC, per-request ownership checks baked into
  repository queries, bcrypt password hashing + strength validation,
  Redis-backed distributed rate limiting, security headers, Workload
  Identity + least-privilege GSAs, non-root hardened containers,
  private GCS bucket + expiring signed URLs) was inspected, found
  already correctly implemented in prior phases, and **left
  unchanged** — see each file below for what was actually checked, not
  assumed.
