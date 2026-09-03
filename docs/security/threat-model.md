# Threat Model (Phase 10)

STRIDE-structured, scoped to the assets the Phase 10 brief names.
"Mitigated" means a specific, inspected control exists (cited);
"Partially mitigated" and "Not mitigated" are recorded honestly, not
rounded up.

## Assets

Users & credentials · JWTs (access + refresh) · Files & metadata · GCS
objects · PostgreSQL · Redis · Pub/Sub · GCP IAM · Kubernetes · Secrets
· the new audit trail itself.

## Threats

### Spoofing

| Threat | Mitigation | Status |
|---|---|---|
| Account takeover via credential stuffing | Redis-backed, distributed rate limiting on `/login` (Phase 7); bcrypt hashing means a leaked hash isn't directly usable | Mitigated |
| Account takeover via stolen refresh token | Rotation + **Phase 10: reuse detection revokes the whole session family** | Mitigated (see `authentication.md`) |
| Forged JWT (algorithm confusion, weak secret) | `jose` verifies signature + `iss` + `exp` + `type`; **Phase 10: `python-jose` bumped past known algorithm-confusion CVEs** | Mitigated, contingent on a real deployment overriding the dev-default `JWT_SECRET_KEY` (see `authentication.md`'s remaining-risk note) |
| Spoofed client IP influencing audit/rate-limit decisions | `TrustedProxyMiddleware` only honors `X-Forwarded-For` from configured trusted proxies (`TRUSTED_PROXIES`, `"*"` by default) | Partially mitigated — `"*"` trusts any proxy hop; correct once pinned to real LB CIDRs (documented requirement, not enforced) |

### Tampering

| Threat | Mitigation | Status |
|---|---|---|
| Parameter tampering (`owner_id`, `role` in a request body) | Never read from client input for an authorization decision — see `authorization.md` | Mitigated |
| Audit log tampering (an attacker or insider editing the trail) | No `UPDATE`/`DELETE` code path exists on `AuditLogRepository` | Mitigated at the application layer — a direct DB-level `UPDATE` by someone with raw Postgres access is NOT prevented (no append-only DB constraint/trigger) | 
| Malicious upload content (path traversal via filename, executable disguised as data) | Filename validated (`FileValidationService`), extension blocklist, magic-byte MIME sniffing (not client-declared `Content-Type`) — inspected, pre-existing | Mitigated |

### Repudiation

| Threat | Mitigation | Status |
|---|---|---|
| A user denying they downloaded/deleted a file | **Phase 10: `FILE_DOWNLOAD`/`FILE_DELETE` audit trail**, with actor, IP, timestamp | Mitigated for the events this phase covers; not extended to every mutation (see `audit-logging.md`'s scope note) |
| An admin denying a privileged lookup | **Phase 10: `ADMIN_ACTION` audit trail** | Mitigated for the one admin action that currently exists (`GET /users/{id}`) |

### Information Disclosure

| Threat | Mitigation | Status |
|---|---|---|
| IDOR — reading another user's file/metadata | Ownership baked into every repository query; verified in tests | Mitigated |
| Signed URL leaking to an unauthorized party via logs | Never logged, never persisted (`storage_service.py`, verified) | Mitigated |
| Data exfiltration via an over-broad GCS IAM grant | 6 separate least-privilege GSAs, no `Owner`/`Editor` (`infrastructure.md`) | Mitigated |
| Secrets in source control | `.env`, `k8s/06-secret.yaml` both gitignored and confirmed never tracked; no hardcoded-secret pattern found in a grep sweep | Mitigated, with the caveat that the sweep was manual grep, not a real Gitleaks/Trivy run (`dependency-audit.md`) |
| Sensitive info in error responses | Domain exceptions map to specific handlers with generic messages; unhandled exceptions go through `unhandled_exception_handler` (not inspected line-by-line this phase for every leak path — pre-existing, not re-audited exhaustively) | Assumed correct from prior phases, not independently re-verified this phase |

### Denial of Service

| Threat | Mitigation | Status |
|---|---|---|
| Login/register flooding | Rate limited (Phase 7) | Mitigated |
| **Refresh-token endpoint flooding** | **Phase 10: `/auth/refresh` now rate-limited** (previously unmetered — a real gap this audit found) | Mitigated |
| Redis outage disabling rate limiting | `RATE_LIMIT_FAIL_OPEN` (default `true`) — availability prioritized over strict limiting for a storage platform, a documented trade-off | Mitigated by design choice, not by preventing the outage |
| Cache stampede against Postgres | Single-flight stampede protection (Phase 7) | Mitigated (pre-existing) |
| Oversized upload exhausting memory/storage | `MAX_UPLOAD_SIZE_MB`/`MAX_CHUNKED_UPLOAD_SIZE_GB`, streamed hashing (never full-file-in-memory) | Mitigated (pre-existing) |

### Elevation of Privilege

| Threat | Mitigation | Status |
|---|---|---|
| Vertical (user → admin) | `require_role`, server-side role check on the fresh DB row every request | Mitigated |
| Horizontal (user → another user's resource) | See Information Disclosure/IDOR above | Mitigated |
| Compromised worker escalating via GCP IAM | Each worker's GSA has ONLY the roles its own function needs (`infrastructure.md`'s table) — e.g. the notification worker has zero GCS access even if compromised | Mitigated |
| Compromised Pod escalating via the Kubernetes API | `runAsNonRoot`, dropped capabilities, no `hostPath`/`hostNetwork`/`hostPID`, least-privilege RBAC (app doesn't call the K8s API at all) | Mitigated |
| Cloud IAM compromise (a leaked GSA credential) | Workload Identity — no downloadable key file exists to leak in the first place | Mitigated (structurally, by not having a key that CAN leak) |

## Explicitly out of scope for this phase's threat model

- **Insider misuse of direct Postgres/GCS console access** — the audit
  trail (and every application-layer control) can be bypassed by
  someone with raw infrastructure access. Mitigating this is an IAM/
  break-glass-process question at the organizational level, not
  something application code can solve.
- **SSRF** — NimbusFS makes no outbound requests to a URL derived from
  user input anywhere in the inspected code (GCS/Pub/Sub SDK calls all
  target fixed, configured endpoints). No SSRF surface was found to
  exist, so none is documented as mitigated — there was nothing to
  mitigate.
