| Directory | Responsibility |
|---|---|
| `api/` | HTTP concerns only: routing, request/response wrapping |
| `core/config` | Typed, environment-driven settings |
| `core/security` | Password hashing, JWT issuing/verification |
| `core/server_identity.py` *(Phase 4)* | Per-process identity (hostname/instance ID/PID/version) |
| `core/retry.py` *(Phase 4)* | Generic async retry with exponential backoff + jitter |
| `core/circuit_breaker.py` *(Phase 4)* | In-process breaker guarding Redis calls |
| `core/distributed_lock.py` *(Phase 4)* | Redis-backed mutual-exclusion lock interface |
| `database/` | Engine/session/pool management, health checks |
| `models/` | ORM table definitions |
| `repositories/` | Query/persistence logic per entity |
| `services/` | Business rules, orchestration across repositories (incl. `cache_service.py`, Phase 4) |
| `schemas/` | Input validation & output serialization contracts |
| `dependencies/` | FastAPI `Depends` graph (DI container) |
| `middleware/` | Cross-cutting request/response processing (incl. `idempotency.py`/`rate_limit.py`, Phase 4) |
| `exceptions/` | Domain exceptions + their translation to HTTP |
| `logging/` | Structured logging setup |
| `utils/` | Small stateless helper functions (e.g. path building, Phase 4's trusted-proxy IP resolution) |
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

All endpoints are namespaced under `/api/v1`. Every `POST`/`PUT`/`PATCH`/
`DELETE` endpoint below additionally accepts an optional `Idempotency-Key`
header for safe client retries (Phase 4 — see §13.13); it's omitted from
each table below since it applies uniformly rather than per-route.

**Auth & Users** *(Phase 1)*

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | none | Full app/DB/Redis/storage health status (§13.14) |
| GET | `/ready` | none | Readiness probe — 503 until startup completes / during shutdown drain (§13.14) |
| GET | `/live` | none | Liveness probe — checks nothing external, on purpose (§13.14) |
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
| `INSTANCE_ID` *(Phase 4)* | Optional explicit server identity; falls back to `HOSTNAME` (auto-set by Kubernetes/Cloud Run) |
| `DEPENDENCY_RETRY_ATTEMPTS`/`_BACKOFF_SECONDS`/`_BACKOFF_MAX_SECONDS` *(Phase 4)* | Startup DB/Redis/GCS retry tuning |
| `GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS` *(Phase 4)* | Max wait for in-flight requests to drain on shutdown |
| `IDEMPOTENCY_ENABLED` / `IDEMPOTENCY_KEY_TTL_SECONDS` *(Phase 4)* | `Idempotency-Key` header support toggle + cache TTL |
| `DISTRIBUTED_LOCK_DEFAULT_TTL_SECONDS` *(Phase 4)* | Default TTL for `DistributedLock` |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_REQUESTS_PER_MINUTE` *(Phase 4)* | Placeholder rate limiter — off by default |
| `TRUSTED_PROXIES` *(Phase 4)* | Proxy IPs allowed to set `X-Forwarded-For`/`-Proto`; empty = trust none |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` / `_RESET_TIMEOUT_SECONDS` *(Phase 4)* | Redis circuit breaker tuning |
| `DATABASE_READ_REPLICA_URL` *(Phase 4)* | Optional read-replica connection string; unset = falls back to primary |

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

## 13. Distributed Backend Architecture (Phase 4)

### 13.1 Phase Overview

Phase 4 does not add a new feature surface (no new nouns like "folder" or
"file") — it changes what kind of *process* NimbusFS is. Phases 1–3 built
a correct single-process API. Phase 4 makes that same API correct when
**N copies of the process run simultaneously** behind a load balancer,
any one of which can die at any moment without the client noticing
anything beyond a possibly-retried request. Concretely, this phase adds:
stateless-by-construction request handling, server identity, structured
distributed logging, correlation/trace ID propagation, Redis
infrastructure (cache layer + distributed lock, both unused by business
logic so far — deliberately, see §13.11), database connection
pooling/retry/optimistic-locking, Idempotency-Key support on every
mutating endpoint, three-tier health checks (`/health`, `/ready`,
`/live`), and a startup/shutdown lifecycle that fails fast and drains
gracefully. **No Kubernetes, no GKE, no Pub/Sub, no chunked upload, no
monitoring stack** — those are later phases; this phase is what makes
Phase 5's Kubernetes deployment possible without a rewrite.

### 13.2 Updated Architecture Diagram

```
                          Clients
                             │
                             ▼
                Google Cloud Load Balancer
              (local stand-in: nginx, see docker-compose.yml)
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
   FastAPI #1           FastAPI #2           FastAPI #3
  (instance_id=app1)   (instance_id=app2)   (instance_id=app3)
        │                    │                    │
        └────────────────────┼────────────────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        Shared Redis   Shared PostgreSQL   Google Cloud Storage
     (cache/lock/idem.)  (metadata, ACID)     (file bytes)
```

Every FastAPI instance runs the identical container image, the identical
code, and holds no data the others don't also have access to via one of
the three shared backing stores. Killing any one instance loses zero
data and drops zero committed work — see §13.3.

### 13.3 Distributed Backend Design

Three properties make the fleet above actually work as one logical
service instead of three unrelated ones:

1. **Interchangeability.** A request that lands on instance #2 gets
   byte-identical behavior to the same request landing on #1 or #3 —
   same code, same config (loaded from the same env vars/Secret
   Manager), same view of the same Postgres/Redis/GCS. The load balancer
   is free to route on whatever policy it wants (round-robin, least-conn,
   session-less) because "which instance handled it" is never part of
   the contract.
2. **No sticky sessions, ever.** There is no server-affinity cookie, no
   in-memory session store, nothing that would make "you must talk to
   the same instance again" true. This is what actually lets the load
   balancer's routing decision be simple prefer-least-busy math (see
   `docker/nginx.conf`'s `least_conn`) instead of a session-aware router.
3. **Shared, external state for everything that must survive a single
   instance's death**: PostgreSQL (metadata, source of truth),
   Google Cloud Storage (bytes), Redis (cache/idempotency/locks — all
   optional/best-effort state, see §13.4). Nothing else persists
   anything.

### 13.4 Stateless Design

**Why statelessness enables horizontal scaling**: a stateful server (one
holding sessions, uploaded-but-not-yet-persisted bytes, or an in-memory
cache the rest of the fleet can't see) turns "add another instance" into
a correctness problem — a client whose session lives only on instance #1
breaks the moment the load balancer sends them to #2. A stateless server
turns "add another instance" into a pure capacity problem: start another
identical process, put it behind the load balancer, done. It's also what
makes failure cheap: losing a stateless instance loses nothing but its
in-flight requests (which retry safely — see §13.13 Idempotent APIs);
losing a stateful one loses whatever only it was holding.

NimbusFS enforces this by construction, not convention:

| Never stored on an instance | Where it actually lives |
|---|---|
| Sessions | Nowhere — JWT access/refresh tokens are self-contained and stateless; the only server-side session-adjacent state is `refresh_tokens.jti` revocation, in Postgres (Phase 1) |
| Uploaded file bytes | Google Cloud Storage (Phase 3) — never buffered to local disk; `StorageService` streams to/from GCS directly |
| Metadata | PostgreSQL (Phases 1–3) |
| Cache | Redis (Phase 4 infra; not yet used for metadata — see §13.11) |
| Idempotency records | Redis (Phase 4, `app/middleware/idempotency.py`) |
| Distributed locks | Redis (Phase 4, `app/core/distributed_lock.py`) |
| Rate-limit counters | Redis (Phase 4 placeholder, `app/middleware/rate_limit.py`) |

**The one deliberate exception, and why it's not a violation**: circuit
breaker state (`app/core/circuit_breaker.py`) and server identity
(`app/core/server_identity.py`) live in process memory. Neither is
*application* state — losing either on restart loses nothing a client
can observe as data loss. Circuit breaker state is this instance's own
local, disposable judgment about whether Redis currently looks healthy;
two instances are allowed to disagree momentarily, and both converge
independently once Redis recovers. Putting that judgment IN Redis would
mean a Redis outage corrupts the very mechanism meant to protect callers
from that outage. Server identity (hostname/instance ID/PID) is, by
definition, a property of the process — there's nowhere else for it to
live.

### 13.5 Request Lifecycle

```
Client
  │  (may include Idempotency-Key, X-Correlation-ID, Authorization headers)
  ▼
Load Balancer
  │  picks any healthy instance (least-conn); no session affinity
  ▼
Available FastAPI Instance
  │  RequestContextMiddleware: generate request_id, resolve/echo
  │  correlation_id + trace_id, bind them + server_id into structlog
  │  contextvars, start the active_requests counter and the timer
  │  SecurityHeadersMiddleware / TrustedHostMiddleware / RateLimitMiddleware
  │  IdempotencyMiddleware: replay a cached response, reject a
  │  same-key-in-flight duplicate, or let a fresh request through
  ▼
Authentication
  │  OAuth2PasswordBearer extracts the bearer token; get_current_user
  │  decodes + validates the JWT, re-checks is_active against Postgres,
  │  binds user_id into the same structlog contextvars
  ▼
Business Logic
  │  Service layer (FolderService, MetadataService, FileUploadService, …)
  │  — unchanged from Phases 1–3; Phase 4 adds nothing here except that
  │  FileMetadata updates now carry an optimistic-lock check (§13.10)
  ▼
Database
  │  AsyncSession from the pooled async engine; repository executes
  │  queries; get_db commits once at the request boundary (or rolls back
  │  and re-raises — a StaleDataError here becomes a 409, see §13.14)
  ▼
Google Cloud Storage
  │  (upload/download/replace/permanent-delete routes only) — bytes
  │  stream directly to/from GCS via StorageService, never touching
  │  local disk
  ▼
Response
     IdempotencyMiddleware caches the final response (if eligible) and
     tags a replay with X-Idempotent-Replay; RequestContextMiddleware
     stamps X-Request-ID/X-Correlation-ID/X-Trace-ID/X-Server-ID/
     X-Response-Time-Ms and logs `request_completed`; SecurityHeaders
     adds hardening headers; the response leaves this instance
```

### 13.6 Folder Structure Changes

```
app/
  core/
    server_identity.py    NEW — hostname/instance_id/PID/version snapshot (§13.7)
    retry.py               NEW — generic async retry w/ exponential backoff+jitter
    circuit_breaker.py      NEW — in-process 3-state breaker guarding Redis calls
    distributed_lock.py     NEW — Redis SET-NX/WATCH-based mutual-exclusion lock
  database/
    session.py               EXTENDED — retry-wrapped health check, read-replica
                              engine/get_db_read, is_retryable_db_error()
    redis.py                  EXTENDED — latency-aware health check, retry
    gcs.py                     EXTENDED — check_storage_connection()
  middleware/
    request_context.py        REWRITTEN — correlation/trace IDs, server ID,
                               execution time header, active-request tracking,
                               trusted-proxy client IP
    idempotency.py             NEW — Idempotency-Key replay/duplicate-detection
    rate_limit.py                NEW — Redis-backed fixed-window placeholder
  services/
    cache_service.py           NEW — generic Redis get/set/delete/exists/incr,
                                circuit-breaker-guarded, fails open
  utils/
    network.py                  NEW — trusted-proxy-aware client IP resolution
  schemas/health.py              EXTENDED — ServerInfo, Readiness/Liveness schemas
  api/v1/health/routes.py         EXTENDED — /ready, /live added; /health enriched
  models/file_metadata.py          EXTENDED — lock_version (optimistic locking)
  exceptions/                       EXTENDED — ConcurrentModificationException,
                                     LockAcquisitionException, IdempotencyConflictException
                                     + StaleDataError/CircuitOpenError/RetryExhaustedError handlers
  main.py                             REWRITTEN — fail-fast startup, graceful shutdown,
                                       new middleware/exception-handler registration
alembic/versions/
  0004_distributed_add_optimistic_lock_version.py   NEW
docker/
  nginx.conf                          NEW — local Google Cloud Load Balancer stand-in
docker-compose.yml                     REWRITTEN — 3 app replicas + nginx (§13.2)
tests/
  test_distributed_backend.py          NEW — readiness/liveness, correlation/trace/
                                        server ID, trusted-proxy IP, startup/shutdown
  test_redis_infrastructure.py          NEW — cache, lock, retry, circuit breaker
  test_idempotency.py                    NEW — replay, duplicate-upload prevention
  test_optimistic_locking.py              NEW — concurrent-update StaleDataError
```

Nothing from Phases 1–3 was regenerated; every file above marked
EXTENDED/REWRITTEN keeps its prior behavior for every prior test (all
76 original tests still pass unmodified except `tests/test_health.py`,
updated only because `/health`'s response shape gained fields).

### 13.7 Configuration Changes

New `Settings` fields (see `.env.example` for the full annotated list):
`INSTANCE_ID`, `BUILD_VERSION`, `GIT_COMMIT_SHA`,
`DEPENDENCY_RETRY_ATTEMPTS`/`_BACKOFF_SECONDS`/`_BACKOFF_MAX_SECONDS`,
`GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS`, `IDEMPOTENCY_ENABLED`/`_KEY_TTL_SECONDS`,
`DISTRIBUTED_LOCK_DEFAULT_TTL_SECONDS`, `RATE_LIMIT_ENABLED`/`_REQUESTS_PER_MINUTE`,
`TRUSTED_PROXIES`, `CIRCUIT_BREAKER_FAILURE_THRESHOLD`/`_RESET_TIMEOUT_SECONDS`,
`DATABASE_READ_REPLICA_URL`. All have safe, conservative defaults (rate
limiting and a read replica are both OFF unless explicitly configured),
so an unmodified `.env` behaves exactly like Phase 3 plus the new
health/tracing surface. `app.core.server_identity.SERVER_IDENTITY` is
computed once at process start from `INSTANCE_ID` (explicit) →
`HOSTNAME` (what Kubernetes/Cloud Run set automatically) → a random
fallback — the same `Settings`/`Environment` machinery from Phase 1
(development/testing/staging/production) is untouched, and everything
above resolves the same way whether the process is running under plain
`uvicorn`, Docker, Cloud Run, or (Phase 5) GKE — none of it is
Docker/Cloud-Run/K8s-specific code, only environment-variable-driven
config, which is what makes it portable across all three without change.

### 13.8 Middleware

Execution order, outermost to innermost (see `app/main.py`'s
`create_application` for the full reasoning on each):

1. **RequestContextMiddleware** — correlation/trace/server IDs, execution
   timing, in-flight request counter (drives graceful shutdown).
2. **SecurityHeadersMiddleware** — unchanged from Phase 1, now
   guaranteed to run on every response including rate-limit 429s and
   idempotent replays (it wraps them).
3. **TrustedHostMiddleware** — unchanged from Phase 1 (Host header
   allowlist), now sitting outside rate-limit/idempotency so a forged
   Host header is rejected before either does any work.
4. **RateLimitMiddleware** — placeholder (§13.13's sibling; see
   `app/middleware/rate_limit.py`), off by default.
5. **IdempotencyMiddleware** — see §13.13.
6. **CORSMiddleware** — innermost, unchanged position from Phase 1.

Authentication remains a FastAPI dependency (`get_current_user`), not
middleware — unchanged from Phase 1, and still the layer that binds
`user_id` into the logging contextvars (§13.16).

**Security notes for production (behind a real load balancer/reverse
proxy):**
- **JWT validation / replay protection** — unchanged from Phase 1
  (`app/core/security/tokens.py`): short-lived access tokens, `jti`-based
  refresh-token rotation (a used refresh token is revoked immediately, so
  replaying a captured one fails), `is_active` re-checked against
  Postgres on every request. Phase 4 adds nothing new here by design —
  auth correctness doesn't change just because there are more instances;
  it already worked per-request, and per-request is still exactly how
  many instances there are.
- **Secure headers** — `SecurityHeadersMiddleware`, unchanged, now
  provably applied to every response including short-circuits (see
  ordering above).
- **Trusted hosts** — `TrustedHostMiddleware` (`ALLOWED_HOSTS`), unchanged
  mechanism, positioned to run before rate-limit/idempotency spend any work.
- **Reverse proxy / forwarded headers compatibility** — new this phase:
  `app/utils/network.py::get_client_ip` only trusts `X-Forwarded-For`
  (and `is_forwarded_https` only trusts `X-Forwarded-Proto`) from peers
  listed in `TRUSTED_PROXIES`. Left empty (the default), NimbusFS trusts
  nothing but the raw TCP peer — safe out of the box, but wrong client
  IPs in logs/rate-limiting if actually deployed behind an untrusted-by-
  default proxy without configuring this. Set it to the load balancer's
  real, fixed IP range in staging/production (never `"*"` on the public
  internet — see `docker-compose.yml`'s local-only use of `"*"` for why
  that's acceptable ONLY when nginx is the sole ingress point).
- **HTTPS awareness** — NimbusFS itself never terminates TLS (Cloud
  Run/GCLB/Phase 5's Ingress do); `is_forwarded_https` is what lets
  application code correctly answer "was this originally an HTTPS
  request" despite TLS having been terminated one hop upstream, without
  trusting a spoofable header from an untrusted peer.

### 13.9 Startup Lifecycle

`app/main.py`'s `lifespan`, in order — **any failure here aborts startup
entirely** (the process never starts accepting connections; Cloud
Run/GKE — Phase 5 — see a failed container and don't add it to rotation):

1. Configuration already loaded (module import time).
2. Structured logging already configured (module import time).
3. Middleware/exception handlers already registered (`create_application`
   runs before `lifespan`).
4. **Connect to PostgreSQL** — `check_database_connection(with_retry=True)`,
   retried `DEPENDENCY_RETRY_ATTEMPTS` times with jittered exponential
   backoff (`app/core/retry.py`) before giving up.
5. **Connect to Redis** — same retry treatment.
6. **Verify the GCS bucket is reachable** — same retry treatment
   (`app/database/gcs.py::check_storage_connection`).
7. **`app.state.ready = True`** — only now does `/ready` return 200. A
   freshly started instance behind a load balancer receives zero traffic
   until this line runs (graceful startup — no cold, half-initialized
   instance ever gets a real request).

### 13.10 Shutdown Lifecycle

Triggered by the ASGI server delivering SIGTERM (what Docker/Cloud
Run/Kubernetes all send before killing a container):

1. **`app.state.ready = False`, immediately, first.** `/ready` starts
   returning 503 right away, so the load balancer stops routing new
   traffic to this instance — this ordering is the entire mechanism;
   draining only works if new arrivals stop first.
2. **Drain in-flight requests** — poll `app.state.active_requests`
   (incremented/decremented per-request by `RequestContextMiddleware`)
   up to `GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS`, so a request already being
   handled gets to finish and respond normally instead of being cut off
   mid-flight.
3. **Close the database connection pool** (`engine.dispose()`).
4. **Close the Redis connection pool** (`redis_pool.disconnect()`).
5. **Log shutdown completion.** (structlog writes synchronously to
   stdout on every call — there's no separate log buffer that needs an
   explicit flush.)

**Distributed locks are deliberately NOT released explicitly here.**
Every `DistributedLock` carries a mandatory TTL specifically so a killed
process (SIGKILL, OOM, node eviction — anything that skips graceful
shutdown entirely) can never leave a lock held forever; self-healing via
expiry is a strictly stronger guarantee than "remember to release on the
way out," so there is nothing this step would add.

### 13.11 Redis Infrastructure

Pure infrastructure in this phase — **no metadata caching is
implemented yet**, by design (a future phase decides real cache
keys/invalidation policy with real traffic data in hand):

- **Shared cache layer** (`app/services/cache_service.py`): generic
  `get`/`set`/`delete`/`exists`/`increment` on arbitrary keys, JSON
  helpers, wrapped in a circuit breaker. **Fails open** — a cache
  miss/no-op on Redis outage, never an exception, because nothing about
  correctness may depend on the cache being up.
- **Distributed lock interface** (`app/core/distributed_lock.py`):
  `SET key token NX PX=ttl` to acquire, a `WATCH`/`MULTI`
  compare-and-delete transaction to release (only the token-holder can
  release its own lock), `extend()` for long critical sections.
  **Fails closed** — unlike the cache, mutual exclusion that silently
  stops being mutually exclusive during a Redis outage is worse than an
  outright error, so `acquire()` returns `False`/raises rather than
  pretending to grant a lock it couldn't guarantee. Not called by any
  endpoint yet — no cross-instance critical section exists in the
  current API surface; it's the interface a future phase's background
  job or "only one instance replaces these bytes at a time" logic uses.
- **Connection pool**: one shared `redis.asyncio.ConnectionPool` per
  process (unchanged from Phase 1), with a 5s connect/socket timeout
  added this phase.
- **Health check**: `check_redis_connection()` now returns
  `(healthy, latency_ms)` and supports a retrying mode for startup.
- **Retry logic**: `app/core/retry.py`'s generic helper, used at startup
  and available to any Redis caller.
- **Dependency injection**: `CacheServiceDep` in
  `app/dependencies/providers.py`, mirroring every other service
  provider in the codebase.

### 13.12 Database Improvements

- **Connection pool**: unchanged sizing knobs
  (`DATABASE_POOL_SIZE`/`DATABASE_MAX_OVERFLOW`), now with
  `pool_recycle=1800` added (proactively retires connections before a
  managed proxy like the Cloud SQL Auth Proxy would reset them anyway).
  **Pool sizing is a fleet-wide budget, not a per-instance one**: with
  N=3 replicas and pool_size=10/max_overflow=20, worst case is
  3 × 30 = 90 Postgres connections — this must stay under Postgres's
  `max_connections`, and must be re-derived whenever the replica count
  changes (a connection pooler like PgBouncer is the real fix once N
  grows large — see §13.20).
- **Retry strategy**: `check_database_connection(with_retry=True)`
  retries transient failures (`OperationalError`, connection-level
  `DBAPIError`) with backoff; `is_retryable_db_error()` additionally
  recognizes Postgres SQLSTATE `40001`/`40P01` (serialization
  failure/deadlock) for services that want to retry a whole transaction.
- **Read/write separation (design, infra only)**: `get_db_read()` and a
  `read_engine` bound to `DATABASE_READ_REPLICA_URL` when set, falling
  back to the primary engine when unset (today, always — no endpoint is
  rewired to use it yet). This is the seam a future phase's read-heavy
  endpoints (search, listing) plug into without touching
  `app/database/session.py` again.
- **Transaction management**: unchanged Unit-of-Work at the request
  boundary (`get_db` commits once, or rolls back and re-raises) — Phase
  4 adds nothing here except that what it re-raises can now include a
  `StaleDataError` (see below), translated to `409` like any other
  domain conflict.
- **Optimistic locking**: `FileMetadata.lock_version`
  (`__mapper_args__ = {"version_id_col": lock_version}`) — see §13.14.
- **Idempotency support**: at the API layer, not the database layer —
  see §13.13 (a database-level "idempotent write" would require
  client-supplied keys as a unique constraint per table, which is a much
  heavier lift than one shared middleware for no behavioral difference).
- **Deadlock handling**: `is_retryable_db_error()` is the detection
  primitive; no service currently wraps a call in a retry loop with it
  (none of today's write paths are multi-statement enough to deadlock
  under realistic concurrency) — provided so a future phase's more
  complex transactions (e.g. a multi-row batch move) can adopt it
  directly instead of reinventing SQLSTATE parsing.
- **Future scaling options**: read replicas (infra already in place,
  §above), PgBouncer/connection pooling in front of Postgres once
  replica count makes per-instance pools add up, table partitioning on
  `file_metadata`/`file_versions` by `owner_id` or time if either grows
  very large, and Cloud SQL's built-in HA (regional failover) for the
  primary itself.

### 13.13 API Improvements — Idempotency

Every mutating endpoint (`POST`/`PUT`/`PATCH`/`DELETE`) supports
`Idempotency-Key` automatically — no per-route code (`app/middleware/idempotency.py`
is global and opt-in via the header, DRY by construction):

- **First request** with a given key: claims an "in-progress" marker in
  Redis, runs the handler normally, caches the final response
  (status/body/`Content-Type`) keyed by
  `sha256(auth_header:method:path:idempotency_key)`.
- **Exact retry** (same key, response already cached): the cached
  response is replayed verbatim — the handler never re-runs — tagged
  `X-Idempotent-Replay: true`. This is what makes a client-side network
  retry of `POST /files/upload` safe: the second attempt returns the
  *same* file, not a second upload.
- **Concurrent retry** (same key, first attempt still in flight): `409
  Conflict` immediately — fails **closed** here specifically, because
  letting two concurrent retries both through would defeat the entire
  point (this is the literal "prevent duplicate uploads" case: two
  near-simultaneous retries must not both create a file).
- **Redis unavailable**: fails **open** — the request proceeds
  unguarded rather than blocking all writes on a cache outage.
  Idempotency is a safety net, not a transaction boundary.
- Only `< 500` responses are cached — a `5xx` likely means the operation
  didn't really complete, so the next retry should actually retry, not
  replay a failure forever.

Endpoints that are idempotent **by HTTP semantics alone**, with no
middleware involved: `PUT /files/{id}/replace` (always produces "this
file's current content is X" regardless of retry count) and
`DELETE /files/{id}/permanent` (deleting an already-deleted resource is
a no-op if repeated without a key). `Idempotency-Key` is what extends
that same safety to `POST`, which is not naturally idempotent (each
successful call ordinarily creates a NEW resource).

### 13.14 Health, Readiness, Liveness Endpoints

Three probes, three distinct jobs — conflating them is a common
production mistake this design avoids:

| Endpoint | Answers | Checks | On failure |
|---|---|---|---|
| `GET /api/v1/health` | "How is everything doing?" (for dashboards/humans) | DB, Redis, GCS bucket, latency of each, server identity | `200` always; body reports `status: "degraded"` |
| `GET /api/v1/ready` | "Should the load balancer send me traffic right now?" | `app.state.ready` flag + DB + Redis | `503` — LB should stop routing here |
| `GET /api/v1/live` | "Is this process able to answer HTTP at all?" | Nothing external, on purpose | (would trigger a restart if wired to an orchestrator's liveness probe) |

The liveness/readiness split matters operationally: a liveness probe
answering "no" tells an orchestrator to **kill and restart the
container** — correct for a deadlocked event loop, catastrophically
wrong for "Postgres is briefly down" (restarting every instance fixes
nothing and drops every in-flight request for no benefit). `/live`
therefore checks literally nothing but process aliveness; dependency
outages are `/ready`'s job, and `/ready` failing just removes the
instance from rotation — no restart, no data loss, and it rejoins
automatically the moment dependencies recover.

### 13.15 Distributed Logging

Every log line, everywhere in the call stack, is structured JSON
(`structlog`, unchanged mechanism from Phase 1) and now automatically
carries: `request_id`, `correlation_id`, `trace_id`, `server_id`
(instance ID), `user_id` (once authenticated), plus each event's own
fields (`method`, `path`, `status_code`, `duration_ms`, etc.) —
timestamp is added by the shared processor chain. **Log aggregation
strategy**: this is exactly the shape Google Cloud Logging (or any
JSON-log collector — Loki, Datadog, ELK) ingests natively with zero
custom parsing — every instance's stdout is collected centrally, and
because `request_id`/`correlation_id`/`trace_id` are globally unique and
present on every line, an operator can pull the complete cross-instance
story of one request (or one client-visible operation, across retries)
with a single `WHERE correlation_id = '...'`-style filter, regardless of
which of the N instances handled which part of it.

### 13.16 Correlation IDs & Request Tracing

`RequestContextMiddleware` establishes three distinct IDs per request
(see its docstring for the full per-ID rationale) and binds them into
`structlog.contextvars` — **not** as function parameters threaded
through every service/repository call. This is a deliberate DRY/KISS
choice: contextvars propagate automatically through the whole async call
stack (middleware → dependency → service → repository → storage layer)
for free, so tracing "just works" everywhere without a single service
method knowing tracing exists, and without every method signature
growing a `request_id: str` parameter. `get_current_user`
(`app/dependencies/auth.py`) adds one more binding — `user_id` — once
auth succeeds, using the exact same mechanism. `trace_id` is
deliberately 32 lowercase hex characters — the same shape as a W3C
`traceparent` trace-id — so swapping in real OpenTelemetry
instrumentation later is a drop-in replacement of *how* the ID is
generated, not a new field.

### 13.17 Error Handling

New exception → HTTP mappings this phase (all going through the same
`_envelope()`/`APIResponse` machinery from Phase 1 — no new response
shape):

| Condition | Exception | HTTP |
|---|---|---|
| Two requests raced to update the same `FileMetadata` row | `StaleDataError` (SQLAlchemy) | `409` |
| A distributed lock couldn't be acquired | `LockAcquisitionException` | `409` |
| A dependency's circuit breaker is open | `CircuitOpenError` | `503` |
| A retried operation exhausted every attempt | `RetryExhaustedError` | `503` |
| Same Idempotency-Key already in flight | (handled inline in middleware) | `409` |
| Redis down during a cache/idempotency/rate-limit call | (caught internally) | request proceeds normally (fail open) — see §13.11/13.13 |

**Graceful degradation** is the throughline: Redis outages never become
request failures (cache/idempotency/rate-limit all fail open); GCS/DB
outages DO fail requests (and fail the whole instance's readiness) since
there's no correct way to serve a file request without the file's bytes
or a metadata request without the metadata — degrading "gracefully"
there would mean silently returning wrong data, which is worse than an
honest error.

### 13.18 Testing

Four new test files, all hermetic (no real Postgres/Redis/GCS process
required — `fakeredis`, in-memory SQLite, and the existing `FakeGCSClient`
cover everything):

- `tests/test_redis_infrastructure.py` — cache get/set/delete/exists/increment,
  graceful degradation on a simulated Redis outage, distributed lock
  acquire/release/contention/context-manager/token-ownership/extend,
  retry helper (transient-failure recovery, exhaustion, non-retryable
  exceptions), circuit breaker (opens on threshold, closes on success,
  half-opens after timeout).
- `tests/test_idempotency.py` — cached replay on retry (folder create AND
  file upload — the literal "prevent duplicate uploads" case), no
  protection without the header (existing business-rule conflict fires
  instead), case-insensitive header matching, `409` on a same-key
  request arriving while the first is still in flight.
- `tests/test_optimistic_locking.py` — two independent sessions racing
  to update the same row (`StaleDataError` on the loser), sequential
  updates still incrementing `lock_version` normally with zero conflict.
- `tests/test_distributed_backend.py` — `/live` always `200`; `/ready`
  `200` when started and `503` before startup/during draining;
  correlation ID generated vs. echoed; trace ID generated (valid 32-hex)
  vs. echoed; `X-Server-ID` matches this process; response-time header
  present; request IDs differ per call; security headers still apply to
  `/ready`; trusted-proxy client IP resolution (trusted vs. untrusted
  peer, wildcard, missing header); full `lifespan` startup success,
  fail-fast on an unrecoverable dependency, and shutdown draining an
  in-flight request before completing.

Run everything with `pytest -v` — **124 tests pass** (the 76 from Phases
1–3, unmodified except `test_health.py`'s two assertions updated for
`/health`'s enriched response shape, plus 48 new).

### 13.19 Design Decisions

- **Contextvars over parameter-threading for tracing** — see §13.16.
- **Global, header-driven idempotency middleware over per-route
  decorators** — one implementation instead of N near-duplicates; a
  route gets the guarantee automatically the moment it's a mutating verb.
- **Optimistic locking only on `FileMetadata`, not `Folder`** — this
  phase scopes the change to the model with the clearest concurrent-write
  story today (rename/move/replace all target it); extending the same
  `AuditMixin`-style mixin to `Folder` later is a one-line, low-risk
  follow-up once there's a concrete reason (Phase 4 avoids speculative
  schema churn).
- **Circuit breaker guards Redis only, not Postgres/GCS** — Postgres and
  GCS are hard dependencies with no correct degraded mode (see §13.17);
  wrapping them in a breaker would just add a state machine with no
  useful action to take when it opens. Redis is the one dependency
  that's genuinely optional to correctness.
- **`WATCH`/`MULTI` over a Lua `EVAL` script for lock release** — a
  portable compare-and-delete that works unchanged against both real
  Redis and `fakeredis` (no Lua support) in tests, at the cost of one
  extra round-trip versus a single `EVAL` call — an acceptable trade for
  a lock whose critical sections are expected to be short and
  infrequent, not a hot path.
- **Docker Compose + nginx over jumping straight to Kubernetes for the
  "3 instances behind a load balancer" proof** — Phase 4's brief
  explicitly excludes Kubernetes; nginx's `least_conn` + passive health
  checks demonstrate the identical shape (load-balanced, interchangeable,
  stateless instances) with a tool available today, and translate
  directly to a Kubernetes Service + Ingress in Phase 5 with no
  application-code change.

### 13.20 Performance Considerations

- **Connection pool budget is fleet-wide, not per-instance** (§13.12) —
  the single biggest thing to re-check every time replica count changes;
  getting it wrong means either wasted idle connections or Postgres
  rejecting connections under load.
- **Idempotency/rate-limit middleware add one Redis round-trip each** to
  every guarded request when active (Idempotency-Key present /
  rate-limiting enabled) — both are opt-in-by-default-off or
  opt-in-by-header, so the common case (no header, rate limiting off)
  costs one cheap settings check and nothing else.
- **`BaseHTTPMiddleware` buffers the full response body** to cache it
  for idempotency — already true of every response passing through
  Starlette's `BaseHTTPMiddleware` regardless, so this isn't new
  overhead, but it does mean the idempotency cache is unsuitable for
  very large response bodies as-is (not a concern for this API's JSON
  envelope responses; would matter if a future phase idempotency-guarded
  a streaming download, which nothing does today — downloads are `GET`,
  outside the guarded method set).
- **`pool_recycle=1800`** trades a small reconnect cost every 30 minutes
  per idle connection for avoiding a much worse failure mode (a managed
  proxy silently dropping a connection the pool still thinks is good).
- **Circuit breaker avoids paying a full TCP connect-timeout on every
  request during a Redis outage** — once open, calls short-circuit
  in-process instantly instead of each waiting out `socket_connect_timeout`.

### 13.21 Interview Questions

1. *Why does `/live` never check the database, while `/ready` does?*
   Because a DB outage should remove the instance from load-balancer
   rotation (`/ready` → 503), not kill and restart a perfectly healthy
   process (which `/live` failing would trigger) — restarting fixes
   nothing about the DB being down and drops in-flight work for free.
2. *Two instances update the same file's metadata at the same instant.
   What actually prevents a lost update, and why isn't a distributed
   lock the answer here?* `FileMetadata.lock_version`
   (SQLAlchemy `version_id_col`) — the loser's `UPDATE` matches zero rows
   and raises `StaleDataError` → `409`, with zero coordination needed
   before the write. A distributed lock would also work but costs a
   round-trip on every write (even the overwhelming majority that never
   race) to prevent something the database can already detect for free
   at commit time.
3. *Why does the cache layer fail open but the distributed lock fails
   closed on a Redis outage?* A cache's job is speed, not correctness —
   degrading to "as if there were no cache" is always safe. A lock's job
   IS correctness (mutual exclusion) — degrading it silently would mean
   pretending to guarantee something you can no longer guarantee, which
   is worse than an explicit failure.
4. *Why is idempotency implemented as global middleware instead of a
   decorator on the two or three routes that most obviously need it
   (upload, folder create)?* Every `POST`/`PUT`/`PATCH`/`DELETE` route
   has the same retry-duplication problem in principle, header-gated
   middleware gives every current AND future mutating route the
   guarantee automatically, and it's one implementation to reason about
   and test instead of N near-identical ones.
5. *What actually breaks if `app.state.ready` is set to `True` before
   the Redis/DB checks pass, instead of after?* The load balancer could
   route real user traffic to an instance mid-startup, whose first real
   request would then hit a connection that isn't warmed/verified yet —
   trading a clean "not ready yet" 503 (which the LB retries elsewhere)
   for a confusing mid-request failure on whatever endpoint the user
   happened to hit first.
6. *Why generate a fresh `request_id` on every hop instead of reusing
   the client's `correlation_id` for it?* They answer different
   questions: `correlation_id` identifies the client-visible operation
   (stable across a retry, so a support engineer can find every attempt
   at "the same" request); `request_id` identifies exactly one hop on
   exactly one instance, which is what actually appears once per line in
   that instance's own logs — collapsing them would make a client-side
   retry (or another service calling in) generate log lines with a
   colliding request_id across two entirely different instances.

### 13.22 Phase 4 Completion Checklist

- [x] Stateless backend — no server-local session/file/cache state; all
      shared state lives in Postgres/Redis/GCS (§13.4)
- [x] Shared configuration — environment-driven `Settings`, unchanged
      mechanism, extended with Phase 4 knobs (§13.7)
- [x] Distributed session strategy — stateless JWTs, no sticky sessions
      (§13.3–13.4)
- [x] Redis shared state — connection pool, health check, retry,
      DI (§13.11)
- [x] Health checks — `/health` (§13.14)
- [x] Readiness checks — `/ready` (§13.14)
- [x] Graceful shutdown — drain in-flight requests before pool teardown (§13.10)
- [x] Graceful startup — fail-fast dependency checks before `ready=True` (§13.9)
- [x] Server identification — hostname/instance ID/PID/version/env/build (§13.7, `app/core/server_identity.py`)
- [x] Request correlation — `X-Correlation-ID` (§13.16)
- [x] Distributed logging — structured JSON, every field, every layer (§13.15)
- [x] Request tracing — `X-Trace-ID`, OTel-shaped, contextvar propagation (§13.16)
- [x] Retry strategy — `app/core/retry.py`, used by DB/Redis/GCS startup checks (§13.9, 13.12)
- [x] Idempotent APIs — `Idempotency-Key` middleware, global (§13.13)
- [x] Connection pool optimization — Postgres pool sizing/recycle, Redis pool timeouts (§13.12)
- [x] Distributed lock design — `app/core/distributed_lock.py` (§13.11)
- [x] No Kubernetes/GKE/Pub-Sub/chunked-upload/monitoring-stack introduced
- [x] All 76 Phase 1–3 tests still pass; 48 new Phase 4 tests added (124 total)

## 14. Running Locally (without Docker)

Requires a local PostgreSQL and Redis instance matching your `.env`.

```bash
alembic upgrade head
./scripts/run_dev.sh
# or: uvicorn app.main:app --reload
```

API available at `http://localhost:8000`, docs at `http://localhost:8000/docs`.

## 15. Running with Docker

**Single-instance dev loop** — same as Phases 1–3:

```bash
cp .env.example .env
docker compose up --build postgres redis app1
```

**Full Phase 4 topology** — 3 stateless app instances behind an nginx
load balancer (see §13.2's diagram):

```bash
cp .env.example .env
docker compose up --build
```

This starts Postgres, Redis, `app1`/`app2`/`app3` (each running the
identical image, differing only in `INSTANCE_ID`), and `nginx` listening
on `http://localhost:8000` and load-balancing across all three. Prove
statelessness for yourself:

```bash
# Every request may land on a different instance — compare X-Server-ID:
for i in 1 2 3 4 5; do curl -s -D - http://localhost:8000/api/v1/live -o /dev/null | grep X-Server-ID; done

# Kill one instance mid-traffic — the other two keep serving with zero errors:
docker compose stop app2
curl -s http://localhost:8000/api/v1/health | jq .data.status
```

Apply migrations inside any running instance (they share one database):

```bash
docker compose exec app1 alembic upgrade head
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
- `0004_distributed` — adds `lock_version` to `file_metadata` (optimistic
  concurrency control, see §13.12/13.14)

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
- **Phase 4** (`tests/test_redis_infrastructure.py`, `test_idempotency.py`,
  `test_optimistic_locking.py`, `test_distributed_backend.py` — see §13.18
  for the full breakdown): cache/lock/retry/circuit-breaker infrastructure,
  idempotent-replay + duplicate-upload prevention, concurrent-update
  conflict detection, readiness/liveness probes, correlation/trace/server-ID
  propagation, trusted-proxy IP resolution, and the full startup/shutdown
  lifecycle — all hermetic via `fakeredis` (no real Redis process needed).

## 19. Future Roadmap (Phases 5–15, not yet built)

Kubernetes/GKE deployment (Ingress, HPA, Pods/Services — Phase 5, the
direct continuation of Phase 4's stateless design), chunked/resumable
uploads, sharing & permissions between users, virus scanning integration,
thumbnail generation, full-text content search, Pub/Sub-driven background
workers (a natural consumer of Phase 4's distributed lock — "only one
worker processes this job"), real metadata caching on top of Phase 4's
cache infrastructure, a real rate-limiting policy on top of Phase 4's
placeholder, CI/CD via GitHub Actions, Terraform IaC, Cloud Armor, Cloud
CDN, observability (Cloud Monitoring/Logging dashboards, and wiring
Phase 4's OTel-shaped `trace_id` up to real OpenTelemetry/Cloud Trace).

## 20. Contribution Guide

1. Create a feature branch from `main`.
2. Keep business logic in `services/`, persistence in `repositories/` — never
   in route handlers.
3. Add/extend tests for any behavior change; run `pytest` before opening a PR.
4. Run `alembic revision --autogenerate` for any model change and commit the
   generated migration alongside the model change.
5. Follow existing typing/async/PEP8 conventions.