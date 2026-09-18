# Authorization — `PermissionResolver` (Phase 13)

Source of truth: `app/core/authorization.py`, `app/dependencies/organization.py`,
`app/dependencies/auth.py`.

This document covers Phase 13's resource-level authorization. For the
pre-existing platform-wide RBAC (`UserRole.USER`/`ADMIN`,
`require_role`), see `docs/security/authorization.md` — that system is
unchanged by this phase and lives alongside, not inside, what follows.

## Two role systems, on purpose

| | `UserRole` (Phase 1) | `OrganizationRole` (Phase 13) |
|---|---|---|
| Scope | Platform-wide | Per-organization |
| Values | `USER`, `ADMIN` | `OWNER`, `ADMIN`, `MEMBER`, `VIEWER` |
| Answers | "Is this a platform operator?" | "What can this user do **within this one tenant**?" |
| Where checked | `require_role` | `require_org_role`, `PermissionResolver` |

They are not merged into one enum. A platform `ADMIN` is not
automatically an `OWNER` of every organization — see
`OrganizationService`'s module docstring for why platform-level
operations (create/suspend/reactivate an organization) and
organization-level operations (manage members, quotas, permissions)
are gated by entirely different dependencies, with no code path that
lets one substitute for the other.

## `PermissionResolver` — the one authorization decision-maker

Every route that touches a folder/file that might not be owned by the
caller asks `PermissionResolver.check()`/`.require()` the same
question — never a per-endpoint `if` check reimplementing "is this
mine?" slightly differently. The precedence algorithm, applied
identically everywhere:

```
1. Organization membership          -> not a member at all? DENY, full stop.
2. Organization status              -> SUSPENDED?           DENY, full stop.
3. Resource ownership (owner_id)    -> owns it?              ALLOW (every permission).
4. Organization role: OWNER/ADMIN   -> ALLOW (every permission, org-wide).
5. Direct ResourcePermission grant  -> on the resource ITSELF -> ALLOW if it names this permission.
6. Inherited ResourcePermission     -> on an ANCESTOR FOLDER  -> ALLOW if it names this permission.
7. Otherwise                        -> DENY.
```

Steps 1-2 are enforced earlier, structurally, by
`get_current_organization` itself (a suspended org or a non-member
never reaches a route handler with a valid `CurrentOrganization` at
all) — `PermissionResolver` only ever sees an already-active
membership in an already-active organization. Steps 3-4 are checked
**before** touching `ResourcePermission` at all: an owner, or an
OWNER/ADMIN of the org, is never one missing grant row away from
losing access to their own resource.

## Fail-safe default

Every exit path in `PermissionResolver.check()` that is not an
explicit `True` returns `False`. There is no "unknown, so allow"
branch. A DB error during the check propagates up as a 5xx via the
existing SQLAlchemy exception handler — denying the request by
definition (no 2xx, no bytes served), never silently falling through
to "allow."

## What this phase does not build

- **No explicit-deny grants.** There is no way to say "user X may
  never access resource Y, even though they'd otherwise inherit
  access." NimbusFS has no product requirement for this yet, and
  Phase 13's brief explicitly warns against adding permission
  complexity without a concrete need. See `docs/permissions.md`.
- **No policy engine / rules DSL.** The six `Permission` values and
  the fixed precedence above are the entire authorization model —
  intentionally, not as a placeholder for something more general.

## Verified

- `mypy app`, `ruff check app`, `bandit -r app` all pass with the
  authorization module in place (**MEASURED**, this session).
- Cross-tenant IDOR/forged-ID/privilege-escalation scenarios are
  covered by `tests/test_multi_tenancy.py` (**TESTED**) — see that
  file for the exact scenarios exercised (folder/file access across
  tenants, forged move targets, permission grants to out-of-org
  principals, forged `X-Organization-ID`).
- No load/performance testing of `PermissionResolver` under concurrent
  cross-tenant traffic has been done (**NOT MEASURED**) — the
  precedence algorithm is designed for O(1) extra queries per check
  (steps 3-4 short-circuit before any `ResourcePermission` query runs
  at all), but this is a design claim, not a benchmarked one.
