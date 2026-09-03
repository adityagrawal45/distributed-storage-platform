# Authentication

Source of truth: `app/core/security/tokens.py`, `app/core/security/password.py`,
`app/services/auth_service.py`, `app/repositories/refresh_token_repository.py`,
`app/models/{user,refresh_token}.py`, `app/schemas/user.py`.

## JWT — inspected, found correct, left unchanged

| Check (Phase 10 brief §8) | Status | Where |
|---|---|---|
| Strong signing algorithm | ✅ HS256 by default, configurable via `JWT_ALGORITHM` | `settings.py` |
| Strong secret/key management | ⚠️ see note below | `settings.py` |
| Issuer validation | ✅ `iss` set and verified on decode | `tokens.py::decode_token` |
| Audience validation | N/A — single-audience system (one API, no third-party token consumers) | — |
| Expiration validation | ✅ `jose.jwt.decode` enforces `exp` natively | `tokens.py` |
| Not-before validation | N/A — tokens have no legitimate future-dated use case; not adding an unused claim | — |
| Token type validation | ✅ `type` claim (`access`/`refresh`) checked after decode, prevents a refresh token being used as an access token | `tokens.py::decode_token` |
| Refresh-token handling | ✅ rotation + revocation (see below) | `auth_service.py` |
| Token revocation strategy | ✅ refresh tokens via `jti` + DB row; access tokens are short-lived and NOT server-revocable (standard, accepted JWT trade-off) | — |
| Clock-skew handling | Not explicitly configured — `jose`'s default leeway is 0s. Not changed this phase: no operational incident has ever pointed at clock skew between replicas (all run in GCP, NTP-synced), and adding leeway widens the exp-enforcement window without a demonstrated need. Documented here as a deliberate non-change, not an oversight. | — |

**Secret/key management note**: `JWT_SECRET_KEY` defaults to
`"CHANGE_ME_DEV_ONLY_SECRET_KEY"` in `settings.py`, explicitly labeled
as a dev-only fallback. This is correct for local `docker-compose up`
convenience and is NOT a vulnerability by itself — the real control is
whether a production deployment actually overrides it via a real
secret (Kubernetes Secret / Secret Manager, per `infrastructure.md`).
**No code path in this codebase ever falls back to the default in a
way that couldn't be caught** — there is no environment check that
warns/fails if `ENVIRONMENT=production` and `JWT_SECRET_KEY` is still
the default. This is a real, low-effort improvement identified but
**not implemented this phase** (see `final-report.md`'s remaining
risks) — a startup assertion (`if is_production and JWT_SECRET_KEY ==
default: raise`) would close it cheaply in a future pass without
touching any authentication logic.

## Password hashing — inspected, found correct, left unchanged

`app/core/security/password.py` uses `passlib`'s `CryptContext(schemes=["bcrypt"])`.
Not changed to Argon2id this phase: bcrypt is not broken, is what the
existing password hashes in any real deployment's database are already
encoded with, and `passlib`'s `CryptContext` already supports adding a
second scheme with automatic migration-on-verify (`deprecated="auto"`)
— switching hashing algorithms is a real, well-supported upgrade path
this library was chosen specifically to keep open, but doing it
unprompted, mid-audit, for a scheme that isn't actually broken would
be exactly the kind of unnecessary rewrite the Phase 10 brief's final
rule warns against.

Password strength validation (`app/schemas/user.py::UserCreate.validate_password_strength`)
requires length ≥ 8, one upper, one lower, one digit, one special
character — enforced at the Pydantic schema boundary, before any
service/hashing code runs. Not changed: reasonable, industry-typical
minimums, not arbitrary.

**Brute-force protection**: `/login` and `/register` are both
Redis-backed rate-limited (`RateLimitCategory.LOGIN`/`REGISTER`, Phase
7) — a distributed, cross-replica limiter, not in-process state. This
is the system's actual brute-force defense; there is no separate
account-lockout mechanism (e.g. "5 failed attempts locks the account
for 15 minutes"), which is a legitimate alternative design not
implemented — noted as a documented gap, not silently absent.

## Refresh tokens

**Design (unchanged, already correct)**: the raw JWT refresh token is
never stored — only its `jti` plus `user_id`/`expires_at`/`revoked`
(`app/models/refresh_token.py`). Rotation: every `/auth/refresh` call
revokes the presented token's `jti` and issues a brand-new pair
(`AuthService.refresh`). Logout revokes one specific `jti`.

**Phase 10 hardening — reuse detection now reacts, not just rejects.**
Previously, presenting an already-revoked refresh token (e.g. a stolen
token used after the legitimate owner already rotated past it) was
rejected with the same generic 401 as any other invalid token, and
nothing else happened. Now (`AuthService.refresh`):

1. A revoked-but-otherwise-valid `jti` is recognized as a distinct
   case — a replay, not a garden-variety invalid token.
2. `RefreshTokenRepository.revoke_all_for_user` is called, revoking
   **every** other live refresh token for that user in one statement —
   not just the replayed one. Rationale: there is no way, from the
   replayed token alone, to tell which of the user's other live
   sessions belong to the legitimate holder versus the attacker who
   may hold more than one stolen token from the same theft. Forcing a
   full re-login everywhere is the safe default.
3. A `TOKEN_REVOCATION` audit event is recorded (see `audit-logging.md`),
   including how many other sessions were actually revoked.

See `tests/test_security_phase10.py::test_replaying_a_rotated_refresh_token_revokes_every_other_live_session`
for the exact behavior under test, including the (correct, not a bug)
detail that every subsequent replay of an already-revoked token — not
only the first — independently triggers this reaction and its own
audit row.

**What this phase did NOT add**: device/session tracking (a
`sessions` table with device info, last-used timestamp, user-visible
"revoke this device" UI) — `RefreshToken` already carries the
`user_id`/`created_at`/`expires_at`/`revoked` fields the Phase 10
brief's §11 lists, but there is no session-metadata (device/IP at
issuance) captured on the row itself, and no `GET /users/me/sessions`
endpoint. This is a real, reasonable next step, explicitly scoped out
of this pass to keep the change surface proportionate to the audit
findings — see `final-report.md`'s remaining risks.

## What this phase did NOT build (and why)

- **Password reset / email verification.** No such feature exists in
  the codebase at all (no email-sending capability beyond the
  `LoggingNotificationSender` stub from Phase 8, which only logs "would
  send email"). Building a real password-reset flow (token generation,
  expiry, single-use enforcement, and an actual email provider) is a
  genuine new FEATURE, not a hardening of an existing one — building
  it unprompted would violate the brief's own "do not optimize for
  producing lots of code" / "if it doesn't exist, don't invent it"
  posture. Recorded here as **DESIGNED-only**: if/when this feature is
  built, the token should follow the exact same pattern
  `RefreshToken` already establishes (store a hash/jti, not the raw
  token; single-use via a `used`/`revoked` flag; short expiry).
