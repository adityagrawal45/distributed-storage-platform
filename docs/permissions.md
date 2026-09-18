# Resource-Level Permissions (Phase 13)

Source of truth: `app/models/resource_permission.py`, `app/services/permission_service.py`,
`app/core/authorization.py`, `app/core/enums.py::Permission`/`PrincipalType`/`ResourceType`.

## The six permissions

| Permission | Gates |
|---|---|
| `READ` | Viewing metadata, listing contents |
| `WRITE` | Renaming, moving, replacing content |
| `DELETE` | Trashing/permanently deleting |
| `SHARE` | Creating a `Share` link for the resource |
| `DOWNLOAD` | Downloading bytes / requesting a signed URL |
| `MANAGE` | Granting/revoking permissions on the resource — a superset of every other permission |

Deliberately six, not dozens. Finer splits (e.g. separating "rename"
from "move," both currently under `WRITE`) were considered and
rejected as complexity with no current product requirement driving
it — the same "don't build unrequested complexity" principle applied
throughout Phase 13.

`MANAGE` implies every other permission on that resource, checked
explicitly at read time (`PermissionResolver.check`) rather than
materialized into six separate grant rows at write time — one row,
one meaning, no risk of the six rows drifting out of sync with each
other.

## Grants: who, what, on what

A `ResourcePermission` row is `(organization_id, resource_type,
resource_id, principal_type, principal_id, permission)` — a principal
(`USER` or `GROUP`) granted one permission on one resource (`FOLDER`
or `FILE`). Unique per `(resource_type, resource_id, principal_type,
principal_id, permission)` — re-granting an already-granted permission
is a no-op, not a duplicate row (`ResourcePermissionRepository.grant`
absorbs the unique-index collision via a SAVEPOINT, the same idempotent-
insert pattern Phase 8's `ProcessedEventRepository` established).

`resource_id`/`principal_id` are bare UUIDs, not foreign keys — this
one table spans two resource types and two principal types, and a
single FK column can only ever point at one target table. The
application, not the database, is responsible for confirming the
referenced row exists (`PermissionService._validate_principal` does
this for the principal side before a grant is ever written).

**Grants never cross organizations.** `PermissionService.grant`
validates that a `USER` principal is an *active member of the same
organization* as the resource, and a `GROUP` principal *belongs to*
that organization, before writing the row — a grant naming a user in a
different tenant is rejected (`MembershipNotFoundException`, 404), not
silently written and then never matched at check time. This is
enforced independent of whatever `PermissionResolver` checks at read
time — belt and suspenders against the exact "share my folder with
someone outside my tenant" mistake Phase 13's brief calls out.

## Inheritance: folder → descendant

A grant on a folder cascades to everything under it. This is
implemented by walking the ancestor chain of a resource's folder(s)
via `Folder.path` (the materialized path Phase 2 already maintains)
and checking `ResourcePermission` for a grant on any ancestor —
`_ancestor_paths()` in `app/core/authorization.py`. No recursive SQL,
no second table of "resolved" grants that could drift out of sync with
the source grants — pure string-prefix splitting over a column that is
already kept correct on every rename/move.

There is no *file*-level "share this file and everything derived from
it" concept — files have no descendants. Only folders inherit
outward.

## Groups

`Group` (org-scoped, flat — no nested groups, no cross-org groups) is
a way to grant one permission to many users at once via
`principal_type=GROUP`. `PermissionResolver` unions a user's own
`user_id` with every group they belong to (scoped to the SAME
organization the resource lives in) when looking up grants.

## Explicitly out of scope this phase

- **No explicit-deny grants.** See `docs/authorization.md`.
- **No permission expiry** on a `ResourcePermission` row (unlike
  `Share`, which does expire) — a direct grant is revoked explicitly
  (`PermissionService.revoke`) or not at all.
- **No "permissions I've been granted" listing endpoint** for the
  grantee's own view (`GET /organizations/me/permissions` lists grants
  *on a resource*, for someone who can already manage that resource —
  there is no reverse index "show me every resource granted to me").

## Verified

- `PermissionResolver`'s precedence order and fail-safe default are
  covered by inline reasoning in the module's own docstring
  (**DESIGNED**) and by the cross-tenant tests in
  `tests/test_multi_tenancy.py::test_permission_grant_rejects_a_principal_outside_the_organization`
  (**TESTED**).
- Folder-inheritance correctness under deep nesting (many ancestor
  levels) has not been separately load-tested — only exercised at the
  shallow depths the current test suite's fixtures create
  (**NOT MEASURED**).
