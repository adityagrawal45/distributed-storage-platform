| Directory | Responsibility |
|---|---|
| `api/` | HTTP concerns only: routing, request/response wrapping |
| `core/config` | Typed, environment-driven settings |
| `core/security` | Password hashing, JWT issuing/verification |
| `database/` | Engine/session/pool management, health checks |
| `models/` | ORM table definitions |
| `repositories/` | Query/persistence logic per entity |
| `services/` | Business rules, orchestration across repositories |
| `schemas/` | Input validation & output serialization contracts |
| `dependencies/` | FastAPI `Depends` graph (DI container) |
| `middleware/` | Cross-cutting request/response processing |
| `exceptions/` | Domain exceptions + their translation to HTTP |
| `logging/` | Structured logging setup |
| `utils/` | Small stateless helper functions (e.g. path building) |
| `tests/` | Unit/integration tests, fixtures |

## 4. Database Design

**`users`**

| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | `uuid4`, avoids sequential ID leakage |
| first_name / last_name | VARCHAR(100) | required |
| email | VARCHAR(255) | unique, indexed — login identifier |
| hashed_password | VARCHAR(255) | bcrypt hash, never returned by the API |
| role | ENUM(`user`,`admin`) | native Postgres enum |
| is_active | BOOLEAN | default `true`; supports account suspension |
| is_verified | BOOLEAN | default `false`; reserved for future email verification |
| created_at / updated_at | TIMESTAMPTZ | server-side defaults via `func.now()` |

**`refresh_tokens`**

| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| jti | UUID | unique, indexed — the token's JWT ID, NOT the raw token |
| user_id | UUID (FK → users.id, `ON DELETE CASCADE`) | indexed |
| revoked | BOOLEAN | default `false`; flips on rotation/logout |
| expires_at | TIMESTAMPTZ | mirrors the JWT's own `exp` claim |
| created_at | TIMESTAMPTZ | |

We deliberately never persist the raw refresh token — only its `jti` — so a
database leak cannot be used to forge/replay authentication.

**`folders`** *(Phase 2)*

| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| owner_id | UUID (FK → users.id, `CASCADE`) | indexed |
| parent_folder_id | UUID (FK → folders.id, `CASCADE`), nullable | self-referential; null = top-level |
| name | VARCHAR(255) | |
| path | VARCHAR(4096) | materialized path, e.g. `/Documents/Projects`; indexed |
| level | INTEGER | depth, 0 = top-level; denormalized for cheap reads |
| is_root | BOOLEAN | true iff `parent_folder_id IS NULL` |
| is_deleted / deleted_at / deleted_by | soft-delete trio | trash state (see `SoftDeleteMixin`) |
| created_by / updated_by | UUID (FK → users.id, `SET NULL`), nullable | audit trail (see `AuditMixin`) |
| created_at / updated_at | TIMESTAMPTZ | |

Unique constraint: `(owner_id, parent_folder_id, name)` **partial index** — only enforced
`WHERE is_deleted = false`, so a trashed "Reports" doesn't block creating a new one
in the same location.

**`file_metadata`** *(Phase 2 core + Phase 3 storage columns)*

| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| owner_id | UUID (FK → users.id, `CASCADE`) | indexed |
| folder_id | UUID (FK → folders.id, `CASCADE`), nullable | null = top-level |
| original_filename | VARCHAR(255) | user-facing name |
| stored_filename | VARCHAR(512), unique | per-row unique key reservation (legacy Phase 2 field; distinct from `object_name` — see below) |
| extension | VARCHAR(32), nullable | derived from filename |
| mime_type | VARCHAR(255), nullable | server-detected from content, not trusted from the client |
| size | BIGINT | current size in bytes |
| checksum | VARCHAR(128), nullable, indexed | SHA-256 of the file's content; drives duplicate detection |
| version | INTEGER | current version pointer (full history in `file_versions`) |
| status | ENUM(`reserved`,`active`,`archived`) | `reserved` = metadata-only (Phase 2); `active` = bytes uploaded (Phase 3) |
| **storage_provider** | VARCHAR(32), nullable | `"gcs"` today; abstraction point for a future backend |
| **bucket_name** | VARCHAR(255), nullable | which GCS bucket the object lives in |
| **object_name** | VARCHAR(1024), nullable, indexed (NOT unique) | the actual GCS object key — see "Object Naming" below |
| **public_url** | VARCHAR(2048), nullable | reserved for future use; always `NULL` today (buckets are private, see Security) |
| **storage_class** | VARCHAR(32), nullable | GCS storage class reported after upload (e.g. `STANDARD`) |
| **etag** | VARCHAR(255), nullable | GCS object etag, for optimistic integrity checks |
| **upload_status** | ENUM(`pending`,`completed`,`failed`), indexed | lifecycle of the *bytes*, independent of `status` (the *row*) |
| **uploaded_at** | TIMESTAMPTZ, nullable | when the bytes were confirmed persisted |
| is_deleted / deleted_at / deleted_by | soft-delete trio | |
| created_by / updated_by | UUID, nullable | audit trail |
| created_at / updated_at | TIMESTAMPTZ | |

Unique constraint: `(owner_id, folder_id, original_filename)`, partial on `is_deleted = false`
— same pattern as folders.

**Why `object_name` is NOT unique**: content-based duplicate detection (see
"Duplicate Detection" below) intentionally lets two different `file_metadata`
rows point at the *same* GCS object when their content is byte-identical.
`stored_filename` stays unique per-row (it's a Phase 2 legacy reservation),
but the physical `object_name` it may share is a many-to-one relationship
by design.

**`file_versions`** *(Phase 2)*

| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| file_id | UUID (FK → file_metadata.id, `CASCADE`) | indexed |
| version | INTEGER | unique per `file_id` |
| checksum / size | snapshot at that version | |
| created_at | TIMESTAMPTZ | |

Append-only. `file_metadata.version/checksum/size` always mirrors the latest row
here; this table exists purely to power `GET /metadata/{id}/versions`.

## 5. API Design

All endpoints are namespaced under `/api/v1`.

**Auth & Users** *(Phase 1)*

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | none | App/DB/Redis health status |
| POST | `/auth/register` | none | Create a new user |
| POST | `/auth/login` | none | OAuth2 password flow → access + refresh token |
| POST | `/auth/refresh` | none (valid refresh token) | Rotate refresh token, issue new pair |
| POST | `/auth/logout` | none (valid refresh token) | Revoke a refresh token |
| GET | `/users/me` | Bearer access token | Current user's profile |
| GET | `/users/{id}` | Bearer access token, **admin role** | Any user's profile |

**Folders** *(Phase 2, all require Bearer auth)*

| Method | Path | Description |
|---|---|---|
| POST | `/folders` | Create a folder (top-level if `parent_folder_id` omitted) |
| GET | `/folders` | List child folders of a parent (or top-level if omitted) |
| GET | `/folders/tree` | Full nested folder tree (forest, or rooted at one folder) |
| GET | `/folders/breadcrumb?folder_id=` | Ancestor chain from root to the given folder |
| GET | `/folders/trash` | List trashed folders |
| GET | `/folders/{id}` | Get a folder by ID |
| PUT | `/folders/{id}` | Rename |
| POST | `/folders/{id}/move` | Move to a new parent |
| DELETE | `/folders/{id}` | Soft delete (moves to trash, cascades to descendants) |
| POST | `/folders/{id}/restore` | Restore from trash |
| DELETE | `/folders/{id}/permanent` | Permanently delete (must be trashed first; irreversible) |

**File Metadata** *(Phase 2, all require Bearer auth)*

| Method | Path | Description |
|---|---|---|
| POST | `/metadata` | Create a metadata placeholder (reserves storage slot; no upload) |
| GET | `/metadata/search` | Search/filter/sort/paginate files |
| GET | `/metadata/trash` | List trashed files |
| GET | `/metadata/{id}` | Get file metadata by ID |
| PUT | `/metadata/{id}` | Update metadata (size/checksum change bumps version) |
| POST | `/metadata/{id}/rename` | Rename |
| POST | `/metadata/{id}/move` | Move to a new folder |
| DELETE | `/metadata/{id}` | Soft delete (moves to trash) |
| POST | `/metadata/{id}/restore` | Restore from trash |
| DELETE | `/metadata/{id}/permanent` | Permanently delete (must be trashed first; irreversible) |
| GET | `/metadata/{id}/versions` | Full version history |

**Files — Cloud Storage** *(Phase 3, all require Bearer auth)*

| Method | Path | Description |
|---|---|---|
| POST | `/files/upload` | Multipart upload: bytes → GCS, metadata → Postgres, in one call |
| GET | `/files/{id}/download` | Stream a file's bytes; supports `Range` requests |
| GET | `/files/{id}/signed-url` | Time-boxed V4 signed URL for direct, temporary access |
| PUT | `/files/{id}/replace` | Upload new bytes as the next version |
| DELETE | `/files/{id}/permanent` | Delete bytes from GCS **and** the metadata row (must be trashed first) |

Note the split with `/metadata`: `DELETE /metadata/{id}` (Phase 2) soft-deletes
a row without touching storage — a trashed file's bytes stay recoverable via
`POST /metadata/{id}/restore`. `DELETE /files/{id}/permanent` (Phase 3) is what
actually reclaims storage, and only after the row is already trashed.

<details>
<summary>Files API examples</summary>

```bash
# Upload
curl -X POST http://localhost:8000/api/v1/files/upload \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -F "file=@report.pdf" \
  -F "folder_id=<optional-folder-uuid>"

# Download (streaming, supports Range)
curl -H "Authorization: Bearer $ACCESS_TOKEN" \
  http://localhost:8000/api/v1/files/<file_id>/download -o report.pdf

# Resume/range download
curl -H "Authorization: Bearer $ACCESS_TOKEN" -H "Range: bytes=0-1023" \
  http://localhost:8000/api/v1/files/<file_id>/download

# Signed URL (defaults to SIGNED_URL_EXPIRATION_MINUTES)
curl -H "Authorization: Bearer $ACCESS_TOKEN" \
  "http://localhost:8000/api/v1/files/<file_id>/signed-url?expires_in_minutes=60"

# Replace (new version)
curl -X PUT http://localhost:8000/api/v1/files/<file_id>/replace \
  -H "Authorization: Bearer $ACCESS_TOKEN" -F "file=@report-v2.pdf"

# Permanently delete (must already be trashed via DELETE /metadata/{id})
curl -X DELETE http://localhost:8000/api/v1/files/<file_id>/permanent \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

</details>

**Trash** *(Phase 2)*

| Method | Path | Description |
|---|---|---|
| GET | `/trash` | Combined view: everything currently trashed (folders + files) |

Every response follows the standard envelope:

```json
{
  "success": true,
  "message": "Request completed successfully",
  "data": {},
  "errors": null,
  "timestamp": "2026-07-29T10:00:00Z",
  "request_id": "b3b3b3b3-1234-4567-8901-abcdefabcdef"
}
```

## 6. Authentication Flow

1. **Register** → `POST /auth/register` creates a user with a bcrypt-hashed password.
2. **Login** → `POST /auth/login` (OAuth2 password form: `username`=email, `password`)
   returns a short-lived **access token** (15 min default) and a longer-lived
   **refresh token** (7 days default). The refresh token's `jti` is persisted.
3. **Access protected routes** → send `Authorization: Bearer <access_token>`.
   `get_current_user` decodes the JWT and re-checks `is_active` against the DB
   on every request, so deactivation takes effect immediately.
4. **Refresh** → `POST /auth/refresh` with the refresh token. The server verifies
   the token's `jti` hasn't been revoked, **revokes it**, and issues a brand-new
   access+refresh pair (**rotation**). Replaying an already-used refresh token
   is rejected — this bounds the damage from a leaked refresh token to one use.
5. **Logout** → `POST /auth/logout` revokes the given refresh token's `jti`.
   Access tokens are stateless and simply expire naturally; there is no
   server-side access-token revocation list in Phase 1 by design.
6. **Role-based authorization** → `require_role(UserRole.ADMIN)` is a dependency
   factory used on routes that need it (e.g. `GET /users/{id}`), so authorization
   requirements are visible in the route signature.

## 7. Folder Hierarchy & Materialized Paths *(Phase 2)*

Folders form a self-referential tree via `parent_folder_id`, with no fixed
depth limit. Each folder also stores a **materialized path**
(e.g. `/Documents/Projects/AI`) and a **level** (depth, 0 = top-level),
kept in sync automatically:

- **Create**: `path = parent.path + '/' + name`, `level = parent.level + 1`.
- **Rename**: the folder's own path is rewritten, and every descendant's
  path is rewritten too (prefix replacement), in one repository call.
- **Move**: the folder's path/level are recomputed against the new parent,
  descendants get both a path-prefix rewrite and a level shift.

This trades a bit of extra work on rename/move for very cheap reads —
breadcrumbs, subtree queries, and "is X inside Y" checks are simple string
operations instead of recursive queries.

**Validation enforced before any move/rename lands:**
- No duplicate folder name within the same parent (soft-deleted siblings excluded)
- No moving a folder into itself
- No moving a folder into one of its own descendants (circular reference)
- Folder names reject empty strings, `.`/`..`, and path-separator characters

## 8. Trash & Soft Delete *(Phase 2)*

There is **no separate trash table**. Both `folders` and `file_metadata`
carry `is_deleted` / `deleted_at` / `deleted_by` directly (via
`SoftDeleteMixin`). This means:

- **Delete** = flip `is_deleted = true`. Deleting a folder cascades the
  same flag to every descendant folder (and, once created inside it,
  every descendant file) so a trashed folder's contents move with it.
- **Restore** = flip it back. Restoring a folder restores the descendants
  that were trashed alongside it, without resurrecting anything that was
  independently trashed earlier.
- **Permanent delete** = the *only* operation that issues a real SQL
  `DELETE`, and it requires the item to already be in the trash — you
  cannot skip straight from active to permanently gone.

## 9. Search, Pagination, Sorting, Filtering *(Phase 2)*

`GET /metadata/search` supports all of the following simultaneously:

- **Free-text search (`q`)**: matches the file's own name OR the name of
  the folder it lives in.
- **Filters**: `folder_id`, `extension`, `mime_type`, `version`,
  `is_deleted`, `created_after`/`created_before`, `updated_after`/`updated_before`.
- **Sorting**: `sort_by` (name, created_at, updated_at, size, type) ×
  `sort_order` (asc/desc).
- **Pagination**: `page` / `page_size` (max 100), returning `total`,
  `total_pages`, `has_next`, `has_previous` alongside `items`.

The same `Page[T]` / `PaginationParams` pair is reused by every list-style
endpoint, so pagination behaves identically across the whole API.

## 10. Cloud Storage Architecture *(Phase 3)*

Bytes and metadata are deliberately split across two systems:

```
Client --multipart--> FastAPI --metadata--> PostgreSQL (file_metadata, file_versions)
                          |
                          +--bytes--> Google Cloud Storage (private bucket)
```

`app/services/storage_service.py::StorageService` is the **only** module that
imports `google.cloud.storage` — nothing else touches a `Blob`/`Bucket`
directly. `app/services/file_upload_service.py::FileUploadService` is the
only orchestrator that talks to both `StorageService` and
`FileMetadataRepository` in the same operation, which is where the
two-systems consistency problem is actually solved.

### Object Naming

Uploaded bytes are **never** stored under the user's original filename.
Instead, `StorageService.generate_object_name()` builds:

```
{tenant}/{owner_id}/{year}/{month}/{uuid4}.{extension}
```

Why:
1. **Path/collision safety** — user-controlled filenames can contain path
   traversal segments, unicode tricks, or simply collide with another file
   (two users can both have `"invoice.pdf"`); a UUID guarantees a globally
   unique, injection-safe key regardless of what anyone names their files.
2. **Sharding** — the `{owner}/{year}/{month}/…` prefix gives GCS's internal
   load balancing a natural partition key, and makes "list this user's
   objects from a given month" cheap for future lifecycle/archival tooling.
3. **Stability** — renaming a file (`original_filename`) never requires
   renaming the underlying object; the object name is permanent for the
   object's lifetime, exactly mirroring the `stored_filename` reservation
   Phase 2 already established.

### Upload Flow & Consistency

```
validate folder exists
  -> validate + hash the upload (filename, size, sniffed MIME, extension)
  -> reject duplicate filename-in-folder (before spending a network upload)
  -> check for identical content already uploaded by this owner (dedup)
  -> upload bytes to GCS (skipped if deduped)
  -> persist FileMetadata (status=active) + FileVersion(v1)
```

**Rollback strategy**: if metadata persistence fails *after* a real
(non-deduped) upload already succeeded, the just-uploaded object is deleted
so no orphaned, unreferenced object is left in the bucket. If that rollback
delete itself fails, `RollbackFailedException` is raised (rather than
silently swallowing the error) — an operator being paged on it is better
than silent drift between Postgres and GCS.

### Duplicate Detection

SHA-256 is computed by streaming the upload once (same pass used to sniff
its MIME type). If a non-deleted `FileMetadata` row for the same owner
already has that checksum, the new row is created pointing at the **same**
`object_name` — no bytes are re-uploaded. This is deliberate
content-addressable-storage behavior: it saves storage cost and upload
bandwidth for the common case of the same file landing in two folders, or a
client-side retry. The trade-off — one object can now be referenced by more
than one row — is exactly why permanent delete (below) checks for other
referencing rows before deleting the object itself.

### Download Flow & Signed URLs

`GET /files/{id}/download` streams bytes back to the client via
`StreamingResponse`, chunk by chunk, without buffering the whole file in
memory — real streaming, not `download_as_bytes()` into a response body.
An HTTP `Range` header is honored (returns `206 Partial Content` with
`Content-Range`), so range-friendly clients (video/audio scrubbing, download
managers, resumable downloads) work correctly.

`GET /files/{id}/signed-url` issues a V4 signed URL — a time-boxed
(`SIGNED_URL_EXPIRATION_MINUTES`, overridable per-request), pre-authenticated
link straight to the object in GCS. This is the **only** sanctioned way a
client gets direct object access; the bucket itself is never public (see
Security, below).

### Replace (New Version)

Replacing a file's content uploads to a **brand-new** object name, swaps
`FileMetadata` to point at it, bumps `version`, and only *then* deletes the
old object (skipped if another row still references it via dedup). Doing it
in this order means a crash at any point still leaves either the old
object+row (untouched) or the new object+row (fully switched over)
consistent — never a state where metadata points at bytes that don't exist.

### Deletion Strategy

- **Soft delete** (`DELETE /metadata/{id}`, unchanged from Phase 2) — flips
  `is_deleted`. The GCS object is left alone, so `POST /metadata/{id}/restore`
  can bring the file back with its bytes intact.
- **Permanent delete** (`DELETE /files/{id}/permanent`, Phase 3) — requires
  the row to already be trashed, then deletes the database row and, only if
  no other row still references the same object (dedup safety check),
  deletes the GCS object too.

### Security

- **Private buckets, always.** Uniform bucket-level access, no `allUsers`/
  `allAuthenticatedUsers` IAM bindings, ever. `public_url` exists as a schema
  column for a possible future CDN-fronted use case but is never populated
  today — the only sanctioned access path is a signed URL or an
  authenticated `/files/{id}/download` request.
- **IAM, least privilege.** The app's service account gets
  `roles/storage.objectAdmin` scoped to its one bucket (see GCS Setup
  above), never project-wide `roles/storage.admin`.
- **Signed URLs are time-boxed**, default 15 minutes, capped at 7 days
  (`expires_in_minutes` query param, `le=10080`) — long enough to be useful,
  short enough that a leaked URL (logged, forwarded, cached by a proxy)
  has a bounded blast radius.
- **Input validation happens before any bytes reach GCS**: filename
  (length/characters), size (`MAX_UPLOAD_SIZE_MB`), extension
  (`BLOCKED_EXTENSIONS`), and MIME type are all checked pre-upload — see
  Upload Flow above.
- **MIME type is sniffed from content**, not trusted from the client's
  `Content-Type` header or the filename extension — both are trivially
  spoofable (e.g. renaming `payload.exe` to `photo.jpg`).
- **Virus scanning is a placeholder for a future phase.** No AV engine is
  wired in yet; `FileValidationService` is the intended integration point
  — a scan step would slot in after content-sniffing and before the GCS
  upload call, with the same "reject before any bytes are written" ordering
  everything else here follows.

## 11. Installation

```bash
git clone <repo-url> nimbusfs && cd nimbusfs
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit secrets, especially JWT_SECRET_KEY
```

## 12. Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Purpose |
|---|---|
| `ENVIRONMENT` | `development` / `testing` / `staging` / `production` |
| `JWT_SECRET_KEY` | HMAC signing secret — **must** be overridden in real deployments |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | Token lifetimes |
| `POSTGRES_*` | Database connection |
| `REDIS_*` | Redis connection |
| `CORS_ALLOWED_ORIGINS` | Comma-separated list of allowed origins |
| `LOG_LEVEL` / `LOG_JSON` | Logging verbosity/format |
| `GCS_PROJECT_ID` / `GCS_BUCKET_NAME` | Google Cloud project + bucket for file storage |
| `GCS_CREDENTIALS_PATH` | Service-account key file path; **leave unset** in staging/production (uses Application Default Credentials / Workload Identity instead) |
| `SIGNED_URL_EXPIRATION_MINUTES` | Default signed-URL lifetime; overridable per-request |
| `MAX_UPLOAD_SIZE_MB` | Hard cap on upload size (rejected with `413`) |
| `ALLOWED_MIME_TYPES` | Comma-separated allowlist; empty = allow all except `BLOCKED_EXTENSIONS` |
| `BLOCKED_EXTENSIONS` | Comma-separated extension blocklist (executables/scripts by default) |

### Google Cloud Storage Setup

```bash
# 1. Create the bucket (uniform bucket-level access, never public)
gsutil mb -p <PROJECT_ID> -l <REGION> -b on gs://nimbusfs-files-dev

# 2. Create a service account for local development
gcloud iam service-accounts create nimbusfs-storage \
  --display-name "NimbusFS Storage (dev)"

# 3. Grant it object-level access ONLY to this bucket (not project-wide Storage Admin)
gsutil iam ch \
  serviceAccount:nimbusfs-storage@<PROJECT_ID>.iam.gserviceaccount.com:roles/storage.objectAdmin \
  gs://nimbusfs-files-dev

# 4. For local dev only: export a key and point GCS_CREDENTIALS_PATH at it.
#    Never do this in staging/production — see below.
gcloud iam service-accounts keys create ./gcs-dev-key.json \
  --iam-account nimbusfs-storage@<PROJECT_ID>.iam.gserviceaccount.com
```

**IAM permissions**: grant `roles/storage.objectAdmin` scoped to the single
bucket (via `gsutil iam ch` above), not `roles/storage.admin` on the project
— the app only ever needs to create/read/delete objects inside its own
bucket, never manage buckets or IAM policy itself.

**Credentials in staging/production**: don't ship a key file in the
container image or mount one as a Kubernetes secret. Instead, bind the
GKE service account to the IAM service account via Workload Identity, leave
`GCS_CREDENTIALS_PATH` unset, and `storage.Client()` picks up Application
Default Credentials automatically — this is what makes `app/database/gcs.py`
work unchanged across environments.

## 14. Running Locally (without Docker)

Requires a local PostgreSQL and Redis instance matching your `.env`.

```bash
alembic upgrade head
./scripts/run_dev.sh
# or: uvicorn app.main:app --reload
```

API available at `http://localhost:8000`, docs at `http://localhost:8000/docs`.

## 15. Running with Docker

```bash
cp .env.example .env
docker compose up --build
```

This starts the API, PostgreSQL, and Redis with health checks and a shared
network. Apply migrations inside the running container:

```bash
docker compose exec api alembic upgrade head
```

## 16. Database Migrations (Alembic)

```bash
# Generate a new migration from model changes
alembic revision --autogenerate -m "describe the change"

# Apply all pending migrations
alembic upgrade head

# Roll back one migration
alembic downgrade -1
```

Migrations so far:
- `0001_initial` — `users`, `refresh_tokens`
- `0002_metadata` — `folders`, `file_metadata`, `file_versions`
- `0003_storage` — adds `storage_provider`, `bucket_name`, `object_name`,
  `public_url`, `storage_class`, `etag`, `upload_status`, `uploaded_at` to
  `file_metadata`

## 17. API Documentation

- Swagger UI: `GET /docs`
- ReDoc: `GET /redoc`
- Raw OpenAPI schema: `GET /openapi.json`

(Docs are automatically disabled when `ENVIRONMENT=production`.)

## 18. Testing

Tests run against an isolated in-memory SQLite database — no external
services required.

```bash
pytest -v
```

Coverage includes:
- **Phase 1**: registration, login, protected routes, role-based authorization,
  refresh-token rotation (including replay rejection), logout, health endpoint.
- **Phase 2**: folder CRUD, nested creation, duplicate-name rejection, rename
  with cascading path updates, move (including circular-reference and
  move-into-self rejection), folder tree, breadcrumb, folder/file trash +
  restore + permanent delete, file metadata CRUD, rename, move, version
  bumping on content change, search (filename, folder-name, extension,
  MIME type, deleted status), sorting, pagination, and cross-owner data
  isolation.
- **Phase 3** (`tests/test_file_storage.py`, against an in-memory
  `FakeGCSClient` — see `tests/fakes/fake_gcs.py`, never real GCS): upload
  success, upload into a nonexistent folder (404), duplicate filename in
  the same folder (409), empty file rejection (400), blocked extension
  rejection (415), content-based duplicate detection/deduplication,
  streaming download round-trip, HTTP Range requests (206), downloading a
  metadata-only (never-uploaded) file (404), signed URL generation (default
  and custom expiration), replace (version bump + object swap + old object
  cleanup), permanent delete (object + row removed), permanent-delete
  safety when an object is still shared via dedup, rollback of an orphaned
  object when metadata persistence fails after a successful upload, a
  simulated storage-backend outage (502), and cross-owner access isolation.

## 19. Future Roadmap (Phases 4–15, not yet built)

Chunked/resumable uploads, sharing & permissions between users, virus
scanning integration, thumbnail generation, full-text content search,
Pub/Sub-driven background workers, rate limiting, Redis caching, CI/CD via
GitHub Actions, Terraform IaC, GKE deployment, Cloud Armor, Cloud CDN,
observability (Cloud Monitoring/Logging dashboards).

## 20. Contribution Guide

1. Create a feature branch from `main`.
2. Keep business logic in `services/`, persistence in `repositories/` — never
   in route handlers.
3. Add/extend tests for any behavior change; run `pytest` before opening a PR.
4. Run `alembic revision --autogenerate` for any model change and commit the
   generated migration alongside the model change.
5. Follow existing typing/async/PEP8 conventions.