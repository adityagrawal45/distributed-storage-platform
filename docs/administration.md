# Administration (Phase 13)

Source of truth: `app/api/v1/organizations/routes.py`, `app/services/organization_service.py`,
`app/services/membership_service.py`, `app/repositories/audit_log_repository.py::search`.

## Two administrative authorities, never blurred

- **Platform admin** (`UserRole.ADMIN`, Phase 1, unchanged) — can
  create new organizations, and suspend/reactivate **any**
  organization. Cannot, through any endpoint in this phase, act as a
  member of an organization they don't belong to, grant permissions
  inside one, or read another organization's audit log — that
  capability simply does not exist on the `OrganizationRole` side of
  the model at all, not because of an extra check gating it.
- **Organization admin** (`OrganizationRole.OWNER`/`ADMIN`, scoped to
  one tenant) — manages members, roles, groups, permission grants,
  quota, and can search their **own** organization's audit log. Has no
  authority over any other organization, structurally: every
  organization-scoped route resolves `organization_id` from
  `CurrentOrganization` (the caller's own membership), never from a
  path/body parameter naming a different org.

## Member management

- `POST /organizations/me/members` adds an **existing** NimbusFS user
  by email — there is no invitation-by-email-to-a-new-account flow.
  Looking up a nonexistent email returns the same 404 shape as any
  other "not found," not a distinguishable "no such user" vs. "already
  a member" (see `MembershipService.add_member`'s docstring on this
  — it also does distinguish already-a-member as a 409, which IS
  disclosed, since the caller is already an org admin managing that
  org's own membership, not an outside attacker probing for emails).
- **Last-owner protection**: `change_role`/`remove_member` both check
  `count_active_owners <= 1` before demoting/removing an `OWNER`, and
  raise `LastOwnerException` rather than allow an organization to end
  up with zero owners — a business rule enforced in the service layer,
  not the authorization layer, since it is about resource-integrity,
  not "who is allowed to do this."

## Audit search

`GET /organizations/me/audit` — `AuditLogRepository.search` takes
`organization_id` as a **required**, not optional, parameter; there is
deliberately no "search across all organizations" method anywhere in
this codebase. A platform-wide audit view, if ever needed, would be a
separate, explicitly platform-scoped capability added later, not a
missing `organization_id is None` branch on this one method.
Pagination is bounded (`PaginationParams`, capped page size) — an
audit search can never return an unbounded result set.

## Secure deletion — the honest gap

`OrganizationStatus.DELETED` is a **soft** marker. Nothing in this
phase adds a background job, an endpoint, or any code path that
actually purges a deleted organization's rows or GCS objects. This is
a deliberate scope limit, not an oversight: a real purge operation is
irreversible and needs its own careful design — a grace period, an
export-before-delete step, GCS bulk-delete with backoff and retry, and
a decision about what happens to audit rows referencing the deleted
org's resources (currently `ON DELETE SET NULL`, preserving the audit
trail's own row even if the organization it once pointed at is gone).
A multi-tenancy phase whose job is introducing the *concept* of an
organization is not the place to also invent that operation.

**Current state**: setting `DELETED` blocks all data-plane access
(same mechanism as `SUSPENDED` — `get_current_organization` rejects
both) but a "deleted" organization's rows remain in Postgres and its
objects remain in GCS indefinitely, consuming quota/storage that is no
longer visible to anyone (**IMPLEMENTED**: the access block;
**NOT IMPLEMENTED**: any actual purge).

## What this phase does not build

- No organization-level billing/invoicing.
- No self-service organization deletion by an `OWNER` (only platform
  admin can suspend; nothing currently sets `DELETED` at all — the
  status exists in the enum for a future phase to use).
- No bulk member import (CSV, SSO/SCIM provisioning).
- No per-organization branding/custom domain.

## Verified

- Platform-vs-organization authority separation is exercised by
  `tests/test_multi_tenancy.py::test_organization_me_never_reflects_a_client_supplied_org_id_for_another_tenant`
  (**TESTED**).
- Last-owner protection has unit-level reasoning in
  `MembershipService`'s own code but no dedicated test in this
  session's test additions — existing coverage is limited to what the
  service's own logic guarantees, not exercised end-to-end via HTTP in
  `tests/test_multi_tenancy.py` (**NOT TESTED** end-to-end; **DESIGNED
  and IMPLEMENTED**).
