# Multi-Tenancy (Phase 13)

Source of truth: `app/models/organization.py`, `app/models/organization_membership.py`,
`app/dependencies/organization.py`, `alembic/versions/0007_multi_tenancy_add_organizations_and_tenant_scoping.py`.

## The tenant boundary

`Organization` is the ONLY tenant boundary in NimbusFS. Every resource
that belongs to a tenant (`Folder`, `FileMetadata`, `UploadSession`)
carries a required, non-nullable `organization_id`. `AuditLog` and
`OutboxEvent` carry it too, but nullable — some events (login,
platform-admin actions) have no single owning tenant.

A user is not a tenant. A user can belong to more than one
organization (`OrganizationMembership`, many-to-many), and every piece
of tenant-owned data is scoped by `organization_id`, never by
`owner_id` alone once Phase 13 lands — `owner_id` still exists (it
answers "who owns this specific file *within* the tenant"), but it is
no longer sufficient on its own to answer "can this request see this
row."

## Isolation architecture: application-level, not RLS, not separate DB

Three approaches were considered:

| Approach | Chosen? | Why / why not |
|---|---|---|
| **Application-level filtering** (every query includes `organization_id`) | **Yes** | No new infrastructure. Enforced structurally: every repository method that touches a tenant-owned table takes `organization_id` as a **required** parameter — omitting it is a `TypeError` at the call site, not a silently-unscoped query. Consistent with this codebase's existing `owner_id`-scoped query pattern (Phase 2 onward) — this is the same technique, one more column. |
| **PostgreSQL Row-Level Security (RLS)** | No | Real defense-in-depth, but requires setting a session-local GUC (`SET app.current_org = ...`) on every connection checkout and keeping every future query's RLS policy in sync with every future column — a second, parallel place tenant logic must be kept correct, for a codebase that does not yet have a connection-pooling layer built around per-request session variables. Recorded here as a legitimate hardening step for a *later* phase, not rejected as wrong — see "Known limitation" below. |
| **Separate database/schema per tenant** | No | NimbusFS's tenants are expected to range from "one user's personal org" (the overwhelming majority, see Migration below) to a handful of real multi-member organizations. Provisioning a schema (or database) per tenant for that shape is operational overhead with no corresponding isolation benefit over application-level filtering done correctly, and it breaks cross-tenant platform-admin queries (`docs/administration.md`) that need a single connection to reach every organization's data. |

**Known limitation of the chosen approach**: a repository method that
*forgets* to filter by `organization_id` — a new method added later
without following the existing pattern — is a real tenant-isolation
bug that only code review and tests catch, not the database itself.
RLS would catch this class of bug at the database level; application-
level filtering does not. This is the honest trade-off of the decision
above, not an oversight.

## Tenant-context resolution (Phase 13 §6)

`app/dependencies/organization.py::get_current_organization` resolves
"which tenant is this request acting within" **before** any route
handler body runs:

1. If `X-Organization-ID` is sent, it is validated against the
   caller's own **active** `OrganizationMembership` — a header naming
   an organization the caller does not belong to is rejected
   (`NotOrganizationMemberException`, 403), never silently ignored or
   substituted.
2. If the header is omitted, and the caller belongs to **exactly one**
   organization, that one is used — this is what keeps every
   pre-Phase-13 API call (which never sends the header) working
   unmodified.
3. If the header is omitted and the caller belongs to **more than
   one** organization, the request is rejected
   (`ValidationException`) — there is no "last used" session-sticky
   default. Silently picking one of several organizations for an
   ambiguous request is exactly the kind of implicit tenant-switching
   Phase 13 explicitly says not to build.

A client-supplied `organization_id` in a request **body** or **query
string** is never read as the tenant context anywhere in this
codebase — only the header, cross-checked against real membership.

## Migration strategy — backward compatibility (Phase 13 §40)

Every user that existed before Phase 13 gets exactly one **personal**
organization (`is_personal=True`, unlimited quota,
`OrganizationRole.OWNER`), created automatically:

- **At registration**, going forward: `AuthService.register` calls
  `OrganizationService.create_personal` right after creating the
  `User` row, in the same request.
- **For every pre-existing user**, via the Alembic migration's backfill
  step (`0007_multi_tenancy...py`): one organization + one OWNER
  membership per existing `users` row, then every one of that user's
  existing `folders`/`file_metadata`/`upload_sessions` rows is updated
  to carry that organization's ID.

Because a personal organization has no `X-Organization-ID` header
requirement (rule 2 above), every existing client, upload, download,
and signed URL keeps working with zero code or client change on their
side — this is the whole point of "personal organization," not an
incidental side effect.

## GCS object isolation

`StorageService.generate_object_name` now requires a real `tenant_id`
(the organization ID as a string) as its object-naming prefix —
`{tenant_id}/{owner_id}/{year}/{month}/{filename}` — replacing the
previous hardcoded `"default"` segment. This is **defense in depth,
not the access-control mechanism**: an object name being
tenant-prefixed does not, by itself, stop a signed URL or a direct GCS
read from crossing tenants — the actual boundary is the
`organization_id` check the API enforces before ever issuing a signed
URL or streaming a download (`FileUploadService.get_downloadable_file`
looks the file up scoped to `organization_id`, then and only then
touches storage).

## What Phase 13 deliberately does not build

- No hard-delete/purge path for a `DELETED` organization's data — see
  `docs/administration.md` "Secure deletion."
- No cross-organization data migration/merge tooling.
- No per-tenant encryption-key separation (all tenants share the same
  GCS bucket and the same at-rest encryption configuration).
