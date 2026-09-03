# Authorization — RBAC, Resource-Level Access, User Isolation

Source of truth: `app/core/enums.py::Role`, `app/models/user.py::UserRole`,
`app/dependencies/auth.py::require_role`, every `*_repository.py`'s
owner-scoped query methods.

## RBAC — inspected, found already implemented, extended for audit only

NimbusFS already has a working RBAC system, built in Phase 1, not
introduced this phase:

- `UserRole` (`app/models/user.py`) is a **native Postgres enum**
  column on `users.role` — `USER` | `ADMIN`. An invalid role is
  rejected at the database level, not only in application code.
- `require_role(*allowed_roles)` (`app/dependencies/auth.py`) is a
  FastAPI dependency **factory** — routes declare their requirement at
  the signature level (`Depends(require_role(UserRole.ADMIN))`), not
  via an `if` check buried in a handler body. Currently used on
  `GET /users/{user_id}` (`app/api/v1/users/routes.py`).
- Authorization is enforced **server-side only**. The client-supplied
  JWT DOES embed a `role` claim at issuance (`create_access_token`),
  but `require_role` never reads it — `get_current_user` re-fetches
  the `User` row from Postgres on every request and `require_role`
  checks the fresh `current_user.role` from that row, not the token
  claim. A role change (or account deactivation) therefore takes
  effect on the very next request, not after the access token expires.
  This was already the design (`get_current_user`'s own docstring
  states the `is_active` version of this reasoning) — Phase 10
  confirms it also covers `role`, since both come from the same
  re-fetched row.
- **Extensibility**: adding a third role (the brief's requirement that
  "additional roles can be introduced later without rewriting
  authorization logic") requires exactly one enum member addition to
  `UserRole` plus a migration — `require_role` and every call site
  using it are unchanged, since they take `*allowed_roles` generically.

**What Phase 10 changed**: nothing about the RBAC mechanism itself. It
added exactly one new observation point — `GET /users/{user_id}` now
also records an `ADMIN_ACTION` audit event (see `audit-logging.md`) —
without touching the authorization decision itself.

**Never trusted from the client, verified**: `role`, `user_id`,
`owner_id`, and `permissions` are never read from a request body/query
param anywhere in the codebase for an authorization decision — grepped
across every route module. Every `owner_id` used in a query comes from
`current_user.id` (the authenticated identity), never from client
input. `POST /files/upload`'s `folder_id` is client-supplied but is
validated as OWNED by the current user (`FileUploadService._validate_folder`)
before use, not trusted as an authorization boundary itself.

## Resource-level authorization (IDOR prevention)

This is the strongest part of the existing design and the reason a
Phase 10 audit found no real IDOR gap to fix: ownership is not checked
as a separate `if` statement bolted onto each handler — it is baked
directly into the repository query itself. The pattern, universal
across the codebase:

```
FileMetadataRepository.get_active_by_id(file_id, owner_id)
    -> SELECT ... WHERE id = :file_id AND owner_id = :owner_id AND is_deleted = false
```

A file that exists but belongs to someone else and a file that does
not exist at all are **indistinguishable** at the query level — both
return `None`, both surface as a 404, never a 403 that would leak
"this ID is real, you're just not allowed to see it" (the standard
resource-enumeration mitigation). This exact shape is repeated in
`FolderRepository`, `UploadSessionRepository.get_owned`, and every
other owner-scoped lookup — verified by inspection, not assumed from
the phase-narrative in `CONTEXT.md`.

Verified explicitly (not just by code reading) in
`tests/test_security_phase10.py`:
- `test_a_user_cannot_get_a_signed_url_for_another_users_file` — 404,
  not 403 or 200.
- `test_a_user_cannot_permanently_delete_another_users_file` — 404.

Pre-existing coverage (`tests/test_file_storage.py`, `test_folders.py`)
already asserts this pattern across upload/download/rename/move/trash
— not re-tested a second time here to avoid duplicate coverage of the
same code path.

**Chunked uploads** (`ChunkedUploadService`, Phase 6) follow the same
pattern (`UploadSessionRepository.get_owned(session_id, owner_id)`) —
inspected, not re-implemented.

**Workers** (Phase 8/9) have no HTTP-facing authorization surface at
all — they consume Pub/Sub messages, not user requests, and
authenticate to GCP via their own scoped Workload Identity GSA (see
`infrastructure.md`), not a user's JWT. "Worker → unauthorized
resource" (Phase 10 brief §25) is therefore a GCP-IAM-layer question,
not an application-authorization one — see `infrastructure.md`'s IAM
section for the per-worker least-privilege table.

## User data isolation

Every item on the Phase 10 brief's §7 list is covered by the IDOR
pattern above, since "cannot view/download/modify/delete another
user's X" is exactly what an owner-scoped repository query enforces
by construction:

| Item | Enforced by |
|---|---|
| View another user's files | `FileMetadataRepository.get_active_by_id(id, owner_id)` |
| Download another user's files | Same — `FileUploadService.get_downloadable_file` calls it first |
| Modify another user's metadata | `MetadataService` methods all take `owner_id` |
| Delete another user's files | Same pattern, verified in `test_security_phase10.py` |
| Access another user's upload sessions | `UploadSessionRepository.get_owned` |
| Access another user's signed URLs | Signed URL is only ever issued FOR a file already ownership-checked — verified in `test_security_phase10.py` |
| Access another user's folders | `FolderRepository`'s owner-scoped queries |
