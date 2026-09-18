# Secure Sharing Links (Phase 13)

Source of truth: `app/models/share.py`, `app/services/share_service.py`,
`app/api/v1/shares/routes.py`.

## What a share is

A `Share` is a bearer credential granting one specific `Permission` on
one specific resource, to **anyone who holds the raw token** — not
tied to a NimbusFS account. This is the one deliberate, documented
exception to Phase 13's tenant-isolation rule: the entire point of a
share is letting someone *outside* the creating tenant (or with no
account at all) reach one resource.

## Token design

- `secrets.token_urlsafe(SHARE_TOKEN_BYTES)` — 32 bytes (256 bits) of
  CSPRNG entropy by default, URL-safe base64-encoded. `secrets`, not
  `random`: this is a real bearer credential, same category as a
  session token.
- Only `token_hash` (SHA-256 of the raw token) is ever persisted. The
  raw token is returned to the creator **exactly once**, in the
  `POST /shares` response body, and never logged, never re-derivable,
  never shown again — the same "store a hash, not the secret"
  principle `User.hashed_password` already applies to login
  credentials.
- SHA-256, not bcrypt, for the token hash: the token's own 256 bits of
  entropy is the security property (nobody brute-forces a 256-bit
  space one HTTP request at a time), so a fast, indexed-lookup-friendly
  hash is correct here — contrast the *optional password* below, which
  IS bcrypt-hashed, because a human-chosen password has far less
  entropy and needs the deliberately slow hash to resist brute force.

## Redemption — the non-disclosure contract

`GET /shares/redeem/{token}` and `.../download` are **public,
unauthenticated** endpoints. Every failure mode short of a wrong
password collapses into the identical `404
ShareExpiredOrRevokedException`:

- Token doesn't exist
- Token was revoked
- Token expired
- `max_downloads` already reached

All four return the same status code and the same response body. This
is deliberate: a distinguishable response for "revoked" vs. "never
existed" would let an attacker fingerprint which tokens are real. A
**wrong password** on an otherwise-valid, password-protected share is
the one case with its own distinct response
(`InvalidSharePasswordException`, 403) — the share's existence is
already unavoidably confirmed to that caller (they were prompted for a
password in the first place), so there is no oracle left to protect by
also hiding this one.

Verified by `tests/test_multi_tenancy.py::test_revoked_share_and_forged_token_both_return_the_same_404`
and `::test_share_with_wrong_password_is_distinguishable_from_expired_or_revoked` (**TESTED**).

## Revocation and expiry

- `revoked_at` is a nullable timestamp, not a boolean — recording
  *when* a share was revoked is what an audit trail needs to answer
  "was this download before or after revocation."
- Every share has an expiry (`SHARE_DEFAULT_EXPIRATION_HOURS`, capped
  at `SHARE_MAX_EXPIRATION_HOURS`) — there is no "never expires" option.
- `max_downloads` is optional; when set, `download_count` (incremented
  on every successful redemption via `ShareService.record_access`) is
  checked against it on every redemption attempt, never cached.

All three checks (revoked / expired / max-downloads) are evaluated
**fresh on every redemption** directly against Postgres — there is no
cache in front of share validity, precisely to avoid needing to get
cache invalidation right for a security-critical check (Phase 13 §18's
"cache is only ever an optimization," applied by simply not caching
this path at all rather than caching it carefully).

## What creating a share requires

`POST /shares` requires the caller to either **own** the resource or
hold `SHARE`/`MANAGE` on it, per `PermissionResolver` — not "any
member of the organization can share anything." A `VIEWER`'s baseline
grants no `SHARE` permission (see `docs/permissions.md`'s policy
table), so a viewer cannot create a share unless separately granted
`SHARE` on that specific resource.

## What this phase does not build

- **No share-link listing for the redeemer.** A share only exposes the
  one resource it names; there is no "browse everything shared with
  me" view for an anonymous or cross-tenant holder.
- **No folder-share download.** `GET /shares/redeem/{token}/download`
  only serves `ResourceType.FILE` shares; a `FOLDER` share can be
  *redeemed* (its metadata returned) but this phase does not build a
  ZIP-of-folder-contents download.
- **No IP allowlisting / geographic restriction** on redemption.

## Verified

- End-to-end create → redeem → download, across two different tenants,
  is covered by `tests/test_multi_tenancy.py::test_share_redemption_is_public_and_crosses_tenants_by_design`
  (**TESTED**).
- Password protection, correct and incorrect, is covered
  (**TESTED**).
- Rate-limiting of redemption attempts against a password-protected
  share (to slow down password guessing) is **NOT IMPLEMENTED** this
  phase — the existing `RateLimitCategory` middleware is not currently
  applied to `/shares/redeem/*`. Recorded here as an honest gap, not a
  silent omission.
