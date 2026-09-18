# Storage Quotas (Phase 13)

Source of truth: `app/models/organization.py` (quota fields),
`app/services/quota_service.py`, `app/repositories/organization_repository.py::try_reserve_storage`.

## The concurrency problem this solves

The naive implementation — `SELECT storage_used_bytes ...`, compare to
the limit in application code, then `UPDATE storage_used_bytes = ...`
— is a textbook check-then-act race: two concurrent uploads can both
read "under quota" before either writes, and both proceed, putting the
organization over its limit. Phase 13's brief calls this out
explicitly as the anti-pattern to avoid.

## The fix: one atomic, guarded `UPDATE`

`OrganizationRepository.try_reserve_storage` issues a single SQL
statement with the limit check **inside the `WHERE` clause**:

```sql
UPDATE organizations
SET storage_used_bytes = storage_used_bytes + :delta_bytes,
    file_count = file_count + :delta_files
WHERE id = :organization_id
  AND (storage_limit_bytes IS NULL
       OR storage_used_bytes + :delta_bytes <= storage_limit_bytes)
```

Postgres's own row-level locking during this `UPDATE` makes it
impossible for two concurrent reservations to both pass when only one
should. The method returns whether the row was actually updated
(`rowcount > 0`) — `False` means "would have exceeded the limit,"
without ever having read the current value into application code
first. There is no separate `SELECT` anywhere in this path.

## When reservation happens: at completion, not at upload start

Quota is reserved **after** the bytes have actually landed in
storage — immediately before the `FileMetadata` row is persisted, not
when the upload request begins. Reserving at start would either
require reversing a reservation on every possible failure path
(client disconnect, GCS error, validation failure) or risk leaking
reserved-but-never-used quota; reserving at completion means a
reservation attempt only ever happens for bytes that definitely exist,
and a rejected reservation triggers the exact same rollback path
(`_rollback_object`) that a metadata-persistence failure already uses —
from that code's point of view, "quota exceeded" and "the DB write
failed" are the same kind of failure (bytes exist, nothing in Postgres
references them yet), handled identically.

For the chunked-upload path, this is the `_finalize` step (after
Compose, before the `FileMetadata` row is created) — mirroring the
single-shot path's timing exactly, not an earlier point in that
upload's much longer lifecycle.

## Release

`QuotaService.release` is the unconditional counterpart —
`permanent_delete` releases the reserved bytes **only if no other
`FileMetadata` row still references the same GCS object** (reusing the
existing content-deduplication reference-counting check from Phase
3), and `replace_file` reserves/releases only the **size delta**
between old and new content, not the full new size.

## What has no quota (by design)

Every user's personal organization
(`OrganizationService.create_personal`) is created with
`storage_limit_bytes=NULL` — unlimited. This is deliberate: a solo
user's experience before and after Phase 13 must be identical, and
quotas are something an `OWNER` opts into for their own organization
later (`PUT /organizations/me/quota`), never silently imposed by this
migration on an existing user.

## What this phase does not build

- **No per-user quota within an organization** — quota is tracked at
  the organization level only; a `MEMBER` with a huge personal upload
  can consume the whole organization's quota, and there is no
  per-member sub-limit.
- **No soft warning threshold** (e.g. "80% of quota used" notification)
  — only a hard reject at the limit.
- **No quota on member count enforcement path wired to an actual
  block** — `Organization.max_members` exists as a column but nothing
  in this phase's `MembershipService.add_member` checks it. Recorded
  as an honest gap: the column is DESIGNED, not yet IMPLEMENTED.

## Verified

- The atomic-UPDATE approach is a design/code-review claim
  (**DESIGNED**, backed by the SQL above actually being what
  `try_reserve_storage` executes — **IMPLEMENTED**).
- No concurrency test actually fires two simultaneous uploads against
  a near-full quota to confirm only one succeeds under real parallel
  load — the existing test suite is sequential/single-connection
  against SQLite, which cannot exercise Postgres row-locking at all.
  This is **NOT TESTED** under real concurrency; it is a correctness
  argument about the SQL, not a measured result.
