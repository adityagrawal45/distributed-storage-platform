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
  -> persist FileMetadata (status=active) + FileVersion(v1). 

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

## 11. Distributed Backend Architecture *(Phase 4)*

Phase 4 turns NimbusFS from "one FastAPI process" into "N interchangeable
FastAPI replicas behind a load balancer," in preparation for a Kubernetes/
Cloud Run deployment in Phase 5. No Kubernetes, autoscaling, or deployment
manifests exist yet — this phase is purely about making the *application
itself* safe to run that way.

```
                     Clients
                         │
                         ▼
              Google Cloud Load Balancer
                         │
        ┌────────────────┼────────────────┐
        │                │                │
        ▼                ▼                ▼
   FastAPI #1      FastAPI #2      FastAPI #3
        │                │                │
        └────────────────┼────────────────┘
                         │
                  Shared Redis
                         │
                 Shared PostgreSQL
                         │
              Google Cloud Storage
```

### Stateless design

No server may hold state a client's *next* request depends on. Concretely:

| Never stored on a server | Where it actually lives instead |
|---|---|
| Sessions | Nowhere — `access`/`refresh` JWTs (Phase 1) carry all session state |
| Uploaded file bytes | Google Cloud Storage (Phase 3) |
| Temporary upload buffers | Streamed straight through to GCS, never written to local disk |
| Application cache | Redis (shared, Phase 4 plumbing only — no metadata caching yet) |
| In-flight request coordination | Redis (distributed locks, idempotency-key records) |

This is what makes horizontal scaling trivial: since no replica has any
memory a client's next request depends on, a load balancer can route each
request to *any* replica, replicas can be added/removed at will, and a
crashed replica loses nothing but its own in-flight requests (which the
client's own retry — see Idempotency below — makes safe to redo against a
different replica).

### Request flow

```
Client
  -> Load Balancer            (routes to any healthy, ready replica)
  -> FastAPI instance         (any one — fully interchangeable)
  -> TrustedProxyMiddleware   (resolves real client IP/scheme from X-Forwarded-*)
  -> RequestContextMiddleware (request/correlation/trace IDs, structured logging)
  -> Authentication           (JWT verified locally — no session store lookup)
  -> Business logic           (services/repositories — no server-local state)
  -> PostgreSQL                (single shared source of truth)
  -> Google Cloud Storage      (single shared bytes store)
  -> Response                 (X-Request-ID/X-Correlation-ID/X-Server-ID headers)
```

Every hop after the load balancer could have landed on a different replica
without changing the outcome — that's the whole point.

### Server identity

Every replica exposes its own identity (`app/core/server_info.py`) in
every log line and every health-check response:

| Field | Purpose |
|---|---|
| `instance_id` | Random UUID, generated fresh per process start |
| `hostname` | OS hostname — the pod/container name on GKE/Cloud Run |
| `process_id` | OS PID, for correlating with process-level metrics |
| `app_version` / `build_version` / `git_commit` | Exactly which build is running |
| `environment` | development/testing/staging/production |

### Distributed logging & correlation IDs

`RequestContextMiddleware` (`app/middleware/request_context.py`) generates
three distinct IDs per request and binds them into `structlog`'s
contextvars, so every log line anywhere in the call stack — routes,
services, repositories, the GCS storage layer — carries them automatically:

- **`request_id`** — unique per hop, always server-generated, never trusted
  from a client header.
- **`correlation_id`** — unique per end-to-end client operation, honored
  from an inbound `X-Correlation-ID` header if present (so a retried
  operation across multiple HTTP calls can be traced as one story),
  otherwise generated fresh.
- **`trace_id`** — reserved for OpenTelemetry/Cloud Trace integration in a
  future phase; honored from `X-Trace-ID` if supplied, defaults to the
  correlation ID otherwise so the field is always populated pre-OTel.

All three, plus `server_id`, are echoed back as response headers
(`X-Request-ID`, `X-Correlation-ID`, `X-Trace-ID`, `X-Server-ID`,
`X-Response-Time-Ms`) and emitted as JSON via `structlog`
(`app/logging/logger.py`, unchanged from Phase 1) — ready to ship straight
into Google Cloud Logging or any other JSON-log aggregator without a
parsing step; grep any of these IDs across every replica's logs to
reconstruct one request's full journey through a distributed fleet.

### Health, readiness & liveness

Three endpoints with three distinct probe semantics (`app/api/v1/health/routes.py`):

| Endpoint | Checks | Used for |
|---|---|---|
| `GET /health` | DB + Redis + Storage (deep) | Dashboards, humans, monitoring |
| `GET /ready` | DB + Redis + Storage (deep), `503` if any unhealthy | Load balancer / readiness probe — "route traffic here?" |
| `GET /live` | Nothing external — answers instantly | Liveness probe — "is this process alive?" |

Splitting `/ready` from `/live` matters: a slow database should pull a
replica out of the load balancer's rotation (`/ready` -> `503`), but must
never cause an otherwise-healthy process to be killed and restarted (which
is what a failing liveness probe triggers) — killing the process doesn't
fix a slow database, it just adds a restart storm on top of it.

### Startup & shutdown lifecycle

`app/main.py`'s `lifespan()`:

- **Startup**: connect DB -> connect Redis -> verify the GCS bucket exists
  -> log `application_ready`. Each check runs through the shared retry
  policy (`app/core/retry.py`, exponential backoff + full jitter) to
  absorb one transient blip (e.g. Postgres still finishing crash recovery
  during a coordinated restart). If a dependency is still unreachable
  after retrying, startup raises and the process exits non-zero —
  `Settings.FAIL_FAST_ON_STARTUP` (default `true`). A replica that can
  never reach its database should never accept traffic; a crash-looping
  pod is a far clearer operational signal than one silently serving 500s
  forever.
- **Shutdown**: close the Postgres connection pool, close the Redis
  connection pool, log `application_shutdown_complete`. Distributed locks
  are deliberately never explicitly released here — every lock has a
  bounded TTL (`app/core/distributed_lock.py`), so a replica that dies
  uncleanly (`kill -9`, OOM) self-heals within that TTL regardless of
  whether any shutdown hook got to run. In production, uvicorn's own
  `--timeout-graceful-shutdown` (set to >= `SHUTDOWN_GRACE_PERIOD_SECONDS`)
  is what actually stops routing new connections and drains in-flight ones
  before this hook runs — this hook is defense-in-depth, not the primary
  mechanism.

### Redis infrastructure

`app/database/redis.py` provides the shared connection pool every replica
draws from; three Phase 4 features build on it directly:

- **`app/core/distributed_lock.py`** — a `SET NX PX`-based lock with a
  Lua-scripted, token-checked release (so replica A can never release a
  lock replica B has since re-acquired after A's lock expired). TTL-based,
  not renewal-based — a deliberate simplicity trade-off documented in the
  module itself.
- **`app/services/idempotency_service.py`** — see Idempotency below.
- **`check_redis_connection()`** — retry-wrapped health check used by
  `/health`, `/ready`, and startup.

Metadata *caching* is explicitly **not** implemented yet — this phase
prepares the plumbing (pool, DI, health check, retry) only.

### Database improvements

- **Connection pooling**: `pool_size`/`max_overflow`/`pool_pre_ping`/
  `pool_recycle`/`pool_timeout` all explicitly tuned (`app/database/session.py`).
  Scaling ceiling documented inline: total Postgres connections used is
  `replica_count * (pool_size + max_overflow)`, which must stay under
  Postgres's own `max_connections` — a future-phase concern (PgBouncer, or
  raising `max_connections`) once replica count grows past what today's
  defaults allow.
- **Retry strategy**: `check_database_connection()` retries transient
  failures with the shared backoff policy.
- **Deadlock handling**: `run_with_deadlock_retry()` retries a unit of
  work specifically on Postgres's `40P01` deadlock SQLSTATE, and only
  that — any other `OperationalError` fails immediately, unretried.
- **Read/write separation & optimistic locking**: both are *designed*,
  documented in detail in `app/database/session.py`'s module docstring,
  and deliberately **not** wired up yet (no read-replica engine exists;
  no `row_version` column has been added to any model) — doing either
  half-heartedly here would either add operational complexity with no
  current benefit (a replica engine nothing routes to) or conflate two
  unrelated meanings of "version" on `FileMetadata` (see that docstring
  for why `FileMetadata.version`, the file's *content* version, can't
  double as a row-level optimistic-lock counter).

### Idempotent APIs

`POST /files/upload` accepts an optional `Idempotency-Key` header
(`app/services/idempotency_service.py`):

1. Client generates one UUID per *logical* upload attempt and sends it on
   every retry of that same attempt.
2. First request with a given key atomically claims it in Redis (`SET NX`)
   and proceeds; concurrent duplicates (a genuine race between two retries
   in flight) get `409 Conflict` — "already being processed" — rather than
   both executing the upload.
3. A request that completes has its response cached under that key
   (`IDEMPOTENCY_KEY_TTL_SECONDS`, default 24h); a later retry with the
   *same* key replays the exact original response instead of re-uploading.
4. A key reused with a different logical request (different filename/
   folder — see `compute_fingerprint`'s docstring for exactly what's
   fingerprinted and why) is rejected with `422` as a client bug, not
   silently replayed.
5. A request that genuinely fails releases its claim immediately, so the
   same key can be retried right away rather than waiting out the full TTL.

This composes with — but is distinct from — Phase 3's SHA-256 content-based
deduplication: idempotency prevents *duplicate uploads from network
retries*; content dedup prevents *duplicate storage bytes for identical
content*, however it got uploaded.

### Trusted proxies & forwarded headers

Behind Google Cloud Load Balancer, the ASGI server sees the LB as the TCP
peer, not the real client. `TrustedProxyMiddleware`
(`app/middleware/proxy_headers.py`) resolves the real client IP/scheme from
`X-Forwarded-For`/`X-Forwarded-Proto`, but only when the peer is in
`Settings.TRUSTED_PROXIES` (`*` for this phase's scope — pin to real proxy
CIDRs once the network topology is fixed) — never trusted unconditionally,
since that would let any direct client spoof its own IP in logs/rate-limit
keys.

### Retry, circuit breaker & rate-limit seams

- **`app/core/retry.py`** — generic async retry with exponential backoff +
  full jitter (jitter specifically to avoid a thundering herd when many
  replicas reconnect to a recovering dependency simultaneously). Used for
  DB/Redis/Storage health checks and startup verification.
- **`app/core/circuit_breaker.py`** — a minimal per-dependency, in-process
  (deliberately not Redis-shared — see module docstring) three-state
  breaker primitive. Provided as infrastructure this phase; broader
  application beyond the health-check paths is left to whichever future
  phase needs it.
- **`app/middleware/rate_limit.py`** — an explicit no-op placeholder that
  documents the seam a real Redis-backed limiter will occupy (`429` +
  `Retry-After`, keyed by client IP/user ID) — deferred past this phase,
  wired in now so the response contract (`X-RateLimit-*` headers) doesn't
  change later.

### Error handling

Every Phase 4 failure mode gets its own domain exception + handler
(`app/exceptions/`), mapped consistently onto the standard `APIResponse`
envelope: `LockAcquisitionException` (409), `CircuitBreakerOpenException`
(503), `ServiceUnavailableException` (503),
`IdempotencyKeyInProgressException` (409),
`IdempotencyKeyReplayedException` (422) — no endpoint ever returns a raw
stack trace or an inconsistent error shape for these.

## 12. Kubernetes Deployment on GKE *(Phase 5)*

Phase 4 made every replica stateless and interchangeable. Phase 5 is
where that promise gets cashed in: NimbusFS now runs as a
self-healing, autoscaling, zero-downtime-deployable fleet on Google
Kubernetes Engine, fronted by a Google Cloud Load Balancer. All
manifests live in [`k8s/`](k8s/) — every field in every manifest is
commented in place with the *why*, not just the *what*; this section is
the narrative/design layer on top. The step-by-step "how do I actually
deploy this" runbook is [`k8s/README.md`](k8s/README.md).

**Scope note**: this phase deploys the application tier only. Pub/Sub,
background workers, a monitoring/observability stack, and CI/CD
automation are all explicitly out of scope — see §24 "Future Roadmap".
(Chunked uploads — listed here as out-of-scope when this section was
originally written — shipped in Phase 6, §13. Multi-zone HA and
disaster recovery — also originally listed here — shipped in Phase 9,
§16.)

### Updated distributed architecture

```
                          Internet
                              │
                Google Cloud Load Balancer  (global, HTTPS, Google-managed cert)
                              │
                     Kubernetes Ingress      (k8s/15-ingress.yaml — GKE-native)
                              │
                    Kubernetes Service       (k8s/08-service.yaml — ClusterIP + container-native LB)
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
 FastAPI Pod 1          FastAPI Pod 2          FastAPI Pod 3        ← k8s/07-deployment.yaml
 (zone us-central1-a)   (zone us-central1-b)   (zone us-central1-c)    (HPA: 3 → 10 — k8s/09-hpa.yaml)
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
      Cloud SQL PostgreSQL  Memorystore Redis  Google Cloud Storage
      (private IP, Phase 1) (private IP, Phase 4) (Workload Identity, Phase 3)
```

Every arrow into the Pods is now something Kubernetes actively manages
(a Service's Endpoints list, updated live as readiness changes) rather
than something a human wires up once and hopes stays correct.

### GKE cluster design

| Component | Choice | Why |
|---|---|---|
| **Control plane** | GKE-managed, regional | Google runs and upgrades the API server/etcd/scheduler across 3 zones — no control-plane node for NimbusFS to operate, patch, or lose sleep over. Regional (not zonal) means the control plane itself survives a single-zone outage, matching the Pod-level zone-spreading in `07-deployment.yaml`. |
| **Worker nodes** | Regional node pool (`nimbusfs-app-pool`), autoscaling 1→6 nodes | Nodes are the thing that actually crashes/gets preempted/needs patching — separating the app's node pool from the cluster's default pool means a `gcloud container node-pools upgrade` on one doesn't have to touch the other. |
| **Node pools** | Two: default (system/add-ons) + `nimbusfs-app-pool` (app Pods, via `nodeAffinity` — `07-deployment.yaml`) | Isolates application capacity/quota from cluster add-ons (kube-dns, metrics-server, the GKE Ingress controller itself) — a runaway app Pod can't starve the very control-plane-adjacent add-ons the cluster needs to keep functioning. |
| **Cluster autoscaler** | `--enable-autoscaling --min-nodes 1 --max-nodes 6` per pool | The HPA (Pod count) and Cluster Autoscaler (node count) are two independent, cooperating layers: the HPA decides "I need more Pods," the Cluster Autoscaler decides "there's nowhere to schedule them, add a node." Without the second layer, the HPA's `maxReplicas: 10` would just produce `Pending` Pods once existing nodes are full. |
| **Networking** | VPC-native (`--enable-ip-alias`) | Pods get real, routable VPC IPs (via alias IP ranges) instead of an overlay network — this is what makes container-native load balancing (NEGs, `08-service.yaml`) possible at all: GCLB can address a Pod IP directly. |
| **VPC / subnets** | Single VPC, regional subnet, secondary ranges for Pods + Services | Keeps NimbusFS's cluster on the same VPC as Cloud SQL (private IP) and Memorystore (private IP) — no VPC peering/interconnect needed, just firewall/NetworkPolicy rules (`11-networkpolicy.yaml`). |
| **Private cluster** | `--enable-private-nodes` + `--master-authorized-networks` | Worker nodes get NO public IPs — only reachable from inside the VPC (or via the GCLB, which reaches Pods through the VPC-native path above, not through a node's public IP). This is the cluster-level enforcement of the same "no direct internet exposure" principle `11-networkpolicy.yaml` enforces at the Pod level. |
| **Release channel** | `regular` | Automatic, tested GKE version/patch upgrades — a deliberate trade-off of a little version control for not being the one responsible for tracking Kubernetes CVEs manually. |

### Kubernetes manifest organization

See the table in [`k8s/README.md`](k8s/README.md#manifest-order) for
the full file listing and apply order. Summary of the object types and
what each is *for* (not restating each field — see the manifest files
themselves):

- **Namespace + ResourceQuota + LimitRange** (`00`–`02`) — the
  blast-radius and resource-ceiling boundary everything else lives
  inside.
- **ServiceAccount + Role/RoleBinding** (`03`–`04`) — Workload Identity
  (GCS/Cloud SQL credentials with no key file, ever) + least-privilege
  Kubernetes API access (which the app doesn't currently use at all —
  the Role is deliberately almost empty).
- **ConfigMap + Secret** (`05`–`06`) — the twelve-factor config
  boundary: everything `app/core/config/settings.py` already reads from
  env vars, split by sensitivity.
- **Deployment** (`07`) — the actual workload: 3 replicas, rolling
  strategy, resource requests/limits, three distinct probes, pod
  anti-affinity, security context.
- **Service** (`08`) — stable internal address + container-native load
  balancing wiring.
- **HorizontalPodAutoscaler** (`09`) — 3→10 replica autoscaling on
  CPU+memory, asymmetric scale-up/scale-down behavior.
- **PodDisruptionBudget** (`10`) — protects availability during
  voluntary disruption (node drains/upgrades), not crashes.
- **NetworkPolicy** (`11`) — default-deny + explicit allow-lists
  (zero trust).
- **BackendConfig + FrontendConfig + ManagedCertificate + Ingress**
  (`12`–`15`) — the GKE-specific objects that turn a ClusterIP Service
  into an internet-facing, TLS-terminated, HTTPS-redirecting Google
  Cloud Load Balancer.

### Health probes: three checks, three different failure responses

Directly reuses Phase 4's three health endpoints
(`app/api/v1/health/routes.py`) — this phase didn't need to invent new
ones, only point Kubernetes at the right one for each purpose:

| Probe | Endpoint | Runs | On failure |
|---|---|---|---|
| **Startup** | `/api/v1/live` | Every 3s, up to 30s, only right after container start | Nothing yet — gives Phase 4's fail-fast startup sequence (connect DB/Redis, verify GCS bucket) room to finish before liveness starts counting failures against it |
| **Liveness** | `/api/v1/live` (zero dependency checks) | Every 10s, for the Pod's whole lifetime | 3 consecutive misses → kubelet sends SIGTERM → `terminationGracePeriodSeconds` grace window (app's own `app/main.py` lifespan shutdown runs here) → SIGKILL if still alive → ReplicaSet schedules a replacement Pod |
| **Readiness** | `/api/v1/ready` (real DB/Redis/Storage checks) | Every 5s, for the Pod's whole lifetime | 2 consecutive misses → Pod removed from the Service's Endpoints (traffic stops routing to it) — **Pod is NOT restarted**; the instant it passes again, it's added back and traffic resumes |

The liveness/readiness split is the single most important design
decision in this section: conflating them (using `/health` — which
checks dependencies — for BOTH) would mean a slow Cloud SQL instance
gets "fixed" by Kubernetes restarting perfectly healthy application
Pods in a loop, which does nothing for the database and adds a
self-inflicted restart storm on top of a real outage.

### Resource management & Quality of Service

Every container declares both `requests` (guaranteed minimum) and
`limits` (hard ceiling), with `limits.cpu/memory > requests.cpu/memory`
— this makes every NimbusFS Pod **Burstable** QoS, one of three classes:

- **Guaranteed** (`requests == limits` on every resource, every
  container) — highest eviction priority, never killed for resource
  pressure before Burstable/BestEffort Pods on the same node. Not used
  here: it's the right class for something like a database's own Pod,
  not a horizontally-scaled, bursty HTTP API where over-provisioning
  every replica to its peak need would be wasteful.
- **Burstable** (what NimbusFS uses) — guaranteed `requests`, allowed to
  use spare node capacity up to `limits` when available, evicted before
  Guaranteed Pods but after BestEffort under node pressure. Matches an
  API that's mostly idle but spikes during upload bursts.
- **BestEffort** (no requests/limits at all) — first to be evicted,
  first to be OOM-killed. `02-limitrange.yaml` exists specifically so
  no NimbusFS container can ever land in this class by omission.

`ephemeral-storage` limits are set too (covers `/tmp` scratch space and
the container's writable layers) — without it, a container filling
local disk (e.g. a very large multipart upload spooling before it
streams to GCS) could exhaust a shared node's disk and affect every
other Pod scheduled there, not just itself.

### Network policies: zero trust

`11-networkpolicy.yaml` starts from **default-deny-all** (every Pod,
both directions) and layers explicit allows on top — GCLB health-check
ranges + same-namespace Pods for ingress; DNS + Cloud SQL + Memorystore
+ Google APIs (via Private Google Access) for egress. Nothing reaches a
NimbusFS Pod, and no NimbusFS Pod reaches anything, that isn't on one
of those lists. This requires the cluster to run Dataplane V2 (enabled
at cluster-creation time — see `k8s/README.md`'s cluster setup command)
since NetworkPolicy is not enforced on a GKE cluster by default.

### Pod Disruption Budget

`minAvailable: 2` (against a 3-replica floor) means a **voluntary**
disruption — a node drain during a GKE node upgrade, the Cluster
Autoscaler scaling a node pool down — is blocked by the Eviction API
from ever taking the service below 2 available Pods, forcing routine
maintenance to proceed one Pod at a time. It does nothing for
**involuntary** disruption (a node crash) — nothing can prevent that;
the ReplicaSet's normal self-healing (see below) is the response to
that instead.

### Self-healing & rolling deployments

**Self-healing**: the Deployment's `selector` continuously reconciles
actual running Pods against `replicas: 3`. Delete any one Pod by hand
and the ReplicaSet notices the mismatch (typically within seconds) and
schedules a replacement — no human, no alert, no runbook step required.
`./scripts/k8s-smoke-test.sh --full` demonstrates this directly: it
deletes a live Pod and asserts the ReplicaSet brings the count back to
3 on its own.

**Rolling update**: `maxUnavailable: 0, maxSurge: 1` means a new image
version is deployed by bringing up ONE extra Pod on the new version,
waiting for it to pass readiness, THEN removing one Pod on the old
version — repeated until all 3 are on the new version. At every point
in that sequence, at least 3 Pods are serving traffic; it is safe
specifically because the app is stateless (Phase 4) — old and new Pods
answering requests side-by-side never causes a consistency problem.

**Rollback**: because the image tag is always an immutable version
(never `:latest` — see `07-deployment.yaml`'s comment),
`kubectl rollout undo` can always re-point the Deployment at the
PREVIOUS, distinct image reference and run the exact same rolling
procedure in reverse. `k8s/README.md`'s kubectl reference and
`scripts/k8s-smoke-test.sh --full` both include a live deploy →
rollback walkthrough.

### Observability preparation (not installed)

Per this phase's explicit scope, no monitoring stack is installed —
only the seams for one are prepared:

- `prometheus.io/scrape`/`port`/`path` Pod annotations
  (`07-deployment.yaml`) — ready for a future Prometheus install to
  auto-discover these Pods, pointed at `/api/v1/health` as a
  placeholder until a real `/metrics` endpoint exists.
- Phase 4's structured JSON logs (`structlog`, every line carrying
  `request_id`/`correlation_id`/`trace_id`/`server_id`) are already
  Cloud Logging-ingestible with zero changes — GKE's default Fluentbit
  DaemonSet on Cloud Logging-enabled clusters picks up container stdout
  automatically.
- `trace_id` (Phase 4, defaults to `correlation_id` today) is the
  documented seam for OpenTelemetry — instrumenting it is deliberately
  left for a future phase, not half-implemented here.

### Docker image (`docker/Dockerfile`)

Now the single canonical Dockerfile for both `docker-compose.yml` (dev)
and every GKE-deployed image — see its own header comment for the full
rationale. Summary of what changed this phase:

- Multi-stage build: build toolchain (gcc, `libpq-dev`) never reaches
  the runtime image — smaller image, smaller attack surface.
- Non-root, fixed-UID user (`1000:1000`) — matches
  `07-deployment.yaml`'s `runAsUser`/`runAsGroup` exactly, and is
  required for the namespace's Pod Security "restricted" admission
  profile to allow the Pod to be created at all.
- `HEALTHCHECK` hits `/api/v1/live` (not `/health`) — same
  liveness-vs-readiness reasoning as the Kubernetes probes.
- Exec-form `CMD` — lets SIGTERM reach uvicorn directly so Phase 4's
  graceful-shutdown lifespan hook actually runs during a rolling
  update, instead of being silently skipped by a shell-form `CMD`.
- `GIT_COMMIT`/`BUILD_VERSION` build args, baked in at image-build
  time — feeds directly into Phase 4's `Settings.BUILD_VERSION`/
  `GIT_COMMIT`, so every `/health` response and log line traces back to
  the exact image that produced it.

### CI/CD preparation (not built)

Deliberately not implemented this phase — the intended future shape,
so the seam is documented rather than improvised later:

1. GitHub Actions workflow, triggered on merge to `main`, running
   `pytest` (all 104+ tests) as a gate.
2. On pass: `docker build` (using `docker/Dockerfile`, with
   `GIT_COMMIT`/`BUILD_VERSION` build args from the CI-provided SHA/tag)
   → push to Artifact Registry, tagged with the Git SHA (immutable,
   matching `07-deployment.yaml`'s "never `:latest`" rule).
3. `kubectl set image` (or a GitOps tool watching Artifact Registry) to
   roll the new tag out via the existing rolling-update strategy — no
   new deployment mechanism needed, CI would just be what triggers the
   same `kubectl` commands `k8s/README.md` documents doing by hand today.

### Testing

A Kubernetes cluster isn't something `pytest` can exercise — there's no
in-memory fake for "a real GKE control plane reconciling a Deployment."
Phase 5's tests are therefore `scripts/k8s-smoke-test.sh`, run against a
real (or `kind`/`minikube`) cluster:

- **Read-only checks** (default): namespace/quota/config objects exist,
  Deployment has the expected ready-replica count, a sample Pod is
  Ready and answers `/live`+`/ready` from inside the container, Service
  has live Endpoints, Ingress has an assigned address, HPA is reading
  metrics, PDB reports a healthy count, NetworkPolicies are present.
- **`--full` (opt-in, mutates the running Deployment)**: deletes a live
  Pod and asserts the ReplicaSet recreates it (self-healing); triggers a
  rolling restart and asserts zero-downtime completion; runs
  `rollout undo` and asserts successful rollback.
- **`scripts/k8s-scale-demo.sh`**: drives synthetic load against the
  in-cluster Service and watches the HPA scale replicas up, then back
  down after the load stops (exercising the asymmetric
  stabilization-window behavior in `09-hpa.yaml`).

See `k8s/README.md` §"Deploy" for exact invocation.

### Phase 5 design decisions

- **GKE-native Ingress over an nginx-ingress/other 3rd-party
  controller**: fewer moving parts to operate (Google manages the
  actual load balancer infrastructure), and native integration with
  ManagedCertificate/BackendConfig/NEGs — appropriate for a
  single-cluster, single-cloud deployment; would reconsider for a
  genuinely multi-cloud future.
- **Container-native load balancing (NEGs) over kube-proxy/iptables for
  external traffic**: GCLB routes directly to Pod IPs, skipping an
  extra network hop and giving GCLB accurate per-Pod health visibility
  (via BackendConfig) instead of an aggregated Service-level view.
- **Google-managed TLS certs over cert-manager + Let's Encrypt**: zero
  extra components to install/operate for this phase's scope; revisit
  only if a future phase needs certs for something ManagedCertificate
  can't express (e.g. wildcard certs across many dynamically-created
  subdomains).
- **Soft (`preferred...`) affinity/anti-affinity, not hard
  (`required...`)**: a hard requirement with only 3 replicas across 3
  zones/nodes can leave a Pod permanently `Pending` during routine node
  pool maintenance; soft degrades gracefully to "still scheduled,
  just less optimally spread" instead.
- **`readOnlyRootFilesystem: true` with a single `/tmp` `emptyDir`
  exception**: enforced by the namespace's `restricted` Pod Security
  profile, and independently justified — Phase 4 already guarantees the
  app has no legitimate reason to write to its own container
  filesystem.
- **Manifests as plain numbered YAML, not Helm/Kustomize**: appropriate
  for one Deployment, one environment's worth of config today; revisit
  if/when a second microservice or multiple environments (staging +
  production clusters) make the duplication cost of plain YAML exceed
  the complexity cost of a templating layer.

### Performance considerations

- **Cold start**: a new Pod's total time-to-ready is bounded by the
  startup probe's 30s ceiling (usually much faster — Cloud SQL/
  Memorystore connections typically establish in low hundreds of ms) —
  this is what the HPA's scale-up responsiveness (0s stabilization
  window, `09-hpa.yaml`) is actually waiting on end-to-end.
  `minReadySeconds: 5` adds a small deliberate floor so a Pod that
  becomes Ready then immediately flaps doesn't count as "available"
  during a rollout.
  
- **Connection pool math at scale**: `app/database/session.py`'s pool
  (`DATABASE_POOL_SIZE=10, DATABASE_MAX_OVERFLOW=20`) is PER POD. At
  the HPA's ceiling of 10 Pods, that's up to 300 possible concurrent
  Postgres connections — this must stay under Cloud SQL's configured
  `max_connections`, which is exactly the ceiling documented (and now
  operationally real, not just theoretical) in that file's own
  docstring. Choose a Cloud SQL tier whose `max_connections` covers
  this, or lower `DATABASE_POOL_SIZE`, before relying on the HPA's full
  range in production.

- **GCLB NEG propagation lag**: Service/Deployment changes (a new
  Pod becoming Ready, a Pod being removed) take on the order of
  seconds-to-low-minutes to propagate into GCLB's own view of healthy
  backends — slower than in-cluster kube-proxy convergence. This is why
  `12-backendconfig.yaml`'s `connectionDraining` window and the Pod's
  own `preStop` sleep both exist: both are absorbing the SAME class of
  propagation-lag race, at two different layers of the stack.

- **HPA CPU-based scaling and I/O-bound work**: Phase 3's GCS calls
  run via `asyncio.to_thread` (the SDK is synchronous) — under a
  concurrent upload burst, CPU usage from JSON serialization/thread
  scheduling rises even though each individual request is
  network-bound, which is what makes CPU a meaningful (not just
  convenient) autoscaling signal here rather than a proxy for the wrong
  thing.

### Interview questions this phase's design answers well

1. *"Why does your liveness probe hit a different endpoint than your
   readiness probe?"* — Because a slow dependency should remove a Pod
   from traffic, not restart it; conflating the two turns every
   database blip into a self-inflicted restart storm.
2. *"Your Deployment's `maxUnavailable` is 0 — doesn't that mean you
   can never remove a Pod during rollout?"* — It means Kubernetes must
   bring the replacement up FIRST (bounded by `maxSurge: 1`); safe here
   specifically because the app is stateless, so briefly running N+1
   Pods across two versions causes no consistency problem.
3. *"How does traffic actually stop reaching a Pod you're about to
   kill, given SIGTERM and Service endpoint removal happen
   concurrently, not in order?"* — Two independent absorbers for the
   same race: the container's own `preStop` sleep, and the GCLB
   BackendConfig's `connectionDraining` window, at two different layers
   of the request path.
4. *"Why is your PodDisruptionBudget `minAvailable: 2` and not
   `maxUnavailable: 1`?"* — `minAvailable` is an absolute floor
   regardless of what the HPA has scaled `replicas` to at the moment
   maintenance happens; `maxUnavailable` would instead scale WITH
   replicas, which isn't the guarantee actually wanted.
5. *"What's your blast radius if the JWT_SECRET_KEY Secret leaks?"* —
   Base64 is encoding, not encryption; real protection is GKE's etcd
   envelope encryption (cluster-level, not this-manifest-level) plus
   this namespace's Role granting `get` on that Secret to exactly one
   ServiceAccount — and the documented next step (Secret Manager CSI
   driver) for rotation/audit logging beyond what a static Secret gives.
6. *"Why Burstable QoS instead of Guaranteed for this workload?"* — A
   horizontally-scaled, bursty HTTP API is better served by guaranteeing
   a modest baseline and allowing burst up to a higher limit than by
   over-provisioning every one of up to 10 replicas to peak need.

### Phase 5 completion checklist

- [x] GKE cluster design documented (control plane, node pools, VPC,
      private cluster, cluster autoscaler) — `k8s/README.md`
- [x] Namespace, ResourceQuota, LimitRange
- [x] ServiceAccount (Workload Identity) + Role + RoleBinding
- [x] ConfigMap (non-secret config) + Secret (template + imperative path)
- [x] Deployment: 3 replicas, rolling strategy (`maxUnavailable: 0`,
      `maxSurge: 1`), resource requests/limits, env vars, graceful
      shutdown (`preStop` + grace period), `imagePullPolicy`
- [x] Startup, readiness, and liveness probes, each hitting the correct
      Phase 4 endpoint
- [x] Node affinity (dedicated app pool) + Pod anti-affinity (zone +
      host spread)
- [x] Service (ClusterIP) + container-native load balancing (NEG)
- [x] Ingress: HTTPS, TLS (Google-managed cert), host/path routing,
      forward-compatible with future API versioning
- [x] HorizontalPodAutoscaler: 3→10, CPU + memory metrics, asymmetric
      scale-up/scale-down behavior
- [x] PodDisruptionBudget (`minAvailable: 2`)
- [x] NetworkPolicy: default-deny + explicit ingress/egress allow-lists
- [x] Dockerfile: multi-stage, non-root, slim, healthcheck, exec-form CMD
- [x] Observability annotations prepared (Prometheus scrape hints) —
      stack itself not installed
- [x] CI/CD path documented — not built
- [x] `scripts/k8s-deploy.sh`, `scripts/k8s-smoke-test.sh` (+ `--full`),
      `scripts/k8s-scale-demo.sh`
- [x] `k8s/README.md` deployment runbook + troubleshooting table
- [ ] Pub/Sub, background workers, chunked uploads, monitoring stack,
      CI/CD automation, multi-region, disaster recovery — explicitly
      deferred to later phases, not started

## 13. Large File, Chunked & Resumable Uploads *(Phase 6)*

### Overview

Phase 3's `/files/upload` works for small-to-medium files: the whole
file streams through one request, hashed and validated in one pass.
That breaks down for genuinely large files (multi-GB video, disk
images, datasets) — a single HTTP request spanning minutes is fragile
over real networks, wastes all progress on any failure, and gives a
client no way to parallelize. Phase 6 adds a second, purpose-built
upload path — `/api/v1/uploads/*` — for exactly this case: chunked,
resumable, parallel-safe, and correct across the distributed,
multi-pod backend Phases 4–5 already built. Phase 3's endpoint is
untouched and remains the right choice for small files.

### Current vs. new architecture

Nothing about the Phase 4/5 distributed backend changes — this phase
adds one more feature on top of it:

```
                     Client
                       │
                       ▼
            Google Cloud Load Balancer / K8s Service   (Phase 5, unchanged)
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
     Pod 1           Pod 2          Pod 3            ← any pod, any request, any chunk
        │              │              │
        └──────────────┼──────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼                             ▼
  PostgreSQL                       Redis
  upload_sessions,                 per-session/per-chunk
  upload_chunks                    coordination locks ONLY
  (AUTHORITATIVE state)            (never authoritative)
        │
        ▼
  Google Cloud Storage
  temp chunk objects → Compose → final object   (AUTHORITATIVE bytes)
```

### Upload lifecycle

```
INITIATE  →  UPLOAD (N chunks, any order, any pod, parallel)  →  RESUME (as needed)  →  VERIFY  →  COMPLETE
   │                        │                                        │                    │           │
   ▼                        ▼                                        ▼                    ▼           ▼
POST /uploads      PUT /uploads/{id}/chunks/{n}          GET /uploads/{id} → missing[]  (internal)  POST /uploads/{id}/complete
creates             streams bytes → temp GCS object,                                    chunk +     Compose chunks → final object,
UploadSession       SHA-256'd, stored as UploadChunk                                     final       whole-object SHA-256, create
row, reserves       row (VERIFIED)                                                       checksum    FileMetadata + FileVersion,
final object                                                                             checks      delete temp chunk objects
key
```

A client can call `GET /uploads/{id}` at any point — after a crash,
after switching networks, after the process restarted — and get back
exactly which chunks landed and which didn't, from ANY pod, because
Postgres (not the pod that handled earlier chunks) is what answers.

### GCS upload architecture — why temp-object-per-chunk + Compose

Researched before implementing, per the "prefer GCS-native mechanisms"
requirement. Two GCS-native options exist:

1. **A single `Blob.create_resumable_upload_session()`** — GCS's own
   resumable-upload primitive. Gives resumability for free, but GCS
   tracks ONE persisted-offset cursor per session: it is fundamentally
   a sequential, single-writer stream. Concurrent or out-of-order
   writes to the same session corrupt the upload (confirmed via GCS
   client-library issue trackers). This satisfies "resumable" but
   cannot satisfy "parallel chunk upload" — both required by this
   phase.
2. **Independent temp objects + Compose** (what this phase implements)
   — each chunk uploads as its own ordinary, independent GCS object
   (via the same `StorageService.upload()` Phase 3 already built — a
   chunk is nothing special to GCS). N chunks genuinely upload in
   parallel with zero shared state. At completion,
   `StorageService.compose_objects()` uses GCS's native Compose
   operation (`Blob.compose()`) to concatenate the ordered chunk
   objects into the final object — capped at 32 sources per call, so
   more than 32 chunks compose in batches, recursively, until one call
   produces the final object. This is Google's own documented pattern
   for "reassembling files uploaded as multiple segments
   simultaneously," not an invented mechanism.

**Where bytes flow**: the literal API this phase implements is
`PUT /uploads/{id}/chunks/{n}` — the client PUTs chunk bytes to
NimbusFS, not directly to a GCS-issued URL, because that's the
requested endpoint contract. What IS avoided: buffering more than one
bounded chunk (`CHUNK_MAX_SIZE_BYTES`, default 256 MiB ceiling) in
memory at a time, and touching local disk at all — the route reads the
raw ASGI request stream directly (`_read_body_bounded` in
`app/api/v1/uploads/routes.py`), never via Starlette's `UploadFile`,
whose default behavior can spool large uploads to a temp file on disk
(unacceptable for stateless Kubernetes pods). The whole multi-GB file
never exists in memory or on disk anywhere in the process at once —
only ever one chunk's worth.

### Database design

```
users ──┬──< upload_sessions >──┬── folders (nullable FK)
         │        │              │
         │        │ 1:N          └── file_metadata (nullable FK, set on completion)
         │        ▼
         │   upload_chunks
         │   UNIQUE(upload_id, chunk_number)
         └── (owner_id FK on both tables)
```

**`upload_sessions`**

| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| owner_id | UUID (FK → users.id, CASCADE) | indexed |
| folder_id | UUID (FK → folders.id, SET NULL), nullable | |
| file_id | UUID (FK → file_metadata.id, SET NULL), nullable | set only once COMPLETED |
| filename, mime_type | VARCHAR | |
| total_size, chunk_size, total_chunks | BIGINT / INT / INT | declared at initiate |
| uploaded_bytes | BIGINT | written exactly once, atomically, at completion — **not** incremented per chunk (see Concurrency Control) |
| status | ENUM (`upload_session_status`) | see State Machine below |
| storage_bucket, storage_object | VARCHAR | final destination object key, reserved at initiate |
| gcs_upload_id | VARCHAR, nullable | reserved for a possible future single-session fallback path; unused by the default Compose-based path |
| checksum_algorithm, expected_checksum, actual_checksum | VARCHAR | SHA-256 |
| idempotency_key | VARCHAR, nullable | audit only — replay logic lives in Redis via `IdempotencyService` |
| expires_at, completed_at, cancelled_at | TIMESTAMPTZ | |
| created_by/updated_by/created_at/updated_at | — | `AuditMixin` (not `SoftDeleteMixin` — see Design Decisions) |

**`upload_chunks`**

| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| upload_id | UUID (FK → upload_sessions.id, CASCADE) | indexed |
| chunk_number | INTEGER | 1-indexed |
| size, checksum | BIGINT / VARCHAR | |
| status | ENUM (`upload_chunk_status`: pending/uploaded/verified/failed) | |
| storage_reference | VARCHAR, nullable | the chunk's own temp GCS object key |
| uploaded_at, created_at, updated_at | TIMESTAMPTZ | `updated_at` uses the same Python-side `onupdate` as `AuditMixin` — see its docstring on the real `MissingGreenlet` bug this avoids |

`UniqueConstraint(upload_id, chunk_number)` is the actual
duplicate-chunk guarantee — see Concurrency Control.

Migration: `alembic/versions/0004_chunked_uploads_add_upload_sessions_and_chunks.py`.

### Upload state machine

```
INITIATED ──► UPLOADING ──► COMPLETING ──► COMPLETED  (terminal)
    │             │              │
    │             │              └──► FAILED ──► UPLOADING | CANCELLED | EXPIRED
    │             │
    │             ├──► CANCELLED  (terminal)
    │             └──► EXPIRED    (terminal)
    │
    ├──► CANCELLED  (terminal)
    └──► EXPIRED    (terminal)
```

Enforced centrally by `app/core/upload_state_machine.py::UploadStateMachine`
— every status change anywhere in `ChunkedUploadService` calls
`assert_transition(current, target)` first; nothing mutates `.status`
directly. `COMPLETED → CANCELLED` and `EXPIRED → COMPLETED` are simply
absent from the transition graph (terminal states map to an empty
transition set), so both are rejected by construction, not by a
special-cased `if` somewhere. `FAILED` is deliberately non-terminal —
a failed completion attempt (e.g. a transient compose error) can
transition back to `UPLOADING` to retry, without the client needing to
start an entirely new session.

### Folder structure — new/modified files

Extends the EXISTING layered structure (organized by technical layer —
`api/`, `models/`, `repositories/`, `services/` — not by feature
folder), matching every prior phase rather than introducing a new
`upload/` package that would be inconsistent with the rest of the app:

```
app/
  core/
    upload_state_machine.py     NEW — centralized valid-transition graph (see above)
    config/settings.py           +CHUNK_MIN/MAX/DEFAULT_SIZE_BYTES, MAX_CHUNKS_PER_UPLOAD,
                                  MAX_CHUNKED_UPLOAD_SIZE_GB, UPLOAD_SESSION_EXPIRATION_MINUTES
    enums.py                     +UploadSessionStatus, ChunkStatus
  models/
    upload_session.py            NEW — UploadSession (AuditMixin)
    upload_chunk.py               NEW — UploadChunk (own Python-side updated_at onupdate)
  repositories/
    upload_session_repository.py NEW — get_owned (ownership-scoped fetch)
    upload_chunk_repository.py   NEW — create_or_get_existing (SAVEPOINT-guarded insert),
                                  sum_verified_bytes, list_verified_ordered, delete_all_for_upload
    base.py                       +flush() helper (mutate-in-place persistence within a request)
  services/
    chunked_upload_service.py    NEW — the whole orchestration; see its module docstring for
                                  the full design-decision writeup (GCS architecture, concurrency
                                  model, idempotency, checksums, transaction boundaries)
    storage_service.py            +compose_objects, delete_many, compute_object_checksum
  schemas/
    upload.py                     NEW — UploadInitiateRequest/Response, UploadProgressRead,
                                  ChunkRead, ChunkUploadResponse, UploadCompleteResponse, UploadCancelResponse
  api/v1/uploads/
    routes.py                     NEW — /uploads/* routes (thin; see module docstring)
  exceptions/custom_exceptions.py +9 Phase 6 exceptions, all subclassing already-registered
                                  bases (NotFoundException/ConflictException/ValidationException/
                                  NimbusFSException) — zero new handler functions, zero main.py
                                  changes (see Error Handling below)
  dependencies/providers.py      +UploadSessionRepositoryDep, UploadChunkRepositoryDep,
                                  ChunkedUploadServiceDep
  api/v1/router.py                mounts the new uploads router
alembic/versions/0004_chunked_uploads_add_upload_sessions_and_chunks.py   NEW migration
tests/
  test_chunked_upload.py         NEW — 41 tests (see Testing below)
  fakes/fake_gcs.py               +FakeBlob.compose() (real byte-concatenation, not a call-count mock)
  conftest.py                     +CHUNK_MIN_SIZE_BYTES test-speed override (same pattern as RETRY_*)
scripts/load-test/
  k6-chunked-upload.js            NEW — k6 load test (recommended — native parallel-request support)
  locustfile.py                   NEW — Locust alternative
  README.md                       NEW — how to run, what to observe, what NOT to conclude
```

### API

All under `/api/v1/uploads`, Bearer auth, standard `APIResponse[T]` envelope:

| Method | Path | Purpose |
|---|---|---|
| POST | `/uploads` | Initiate a session (supports `Idempotency-Key`) |
| GET | `/uploads/{id}` | Status/progress — `uploaded_chunks`, `missing_chunks`, `progress_percentage` |
| GET | `/uploads/{id}/chunks` | List all chunk records |
| PUT | `/uploads/{id}/chunks/{n}` | Upload one chunk (body = raw bytes; optional `X-Chunk-Checksum` header) |
| POST | `/uploads/{id}/complete` | Finalize (supports `Idempotency-Key`; safe against duplicate calls regardless) |
| POST | `/uploads/{id}/cancel` | Abort (idempotent) |
| DELETE | `/uploads/{id}` | Cancel-if-active, then hard-delete the session record |

**Example — initiate:**
```json
POST /api/v1/uploads
{"filename": "large-video.mp4", "size": 10737418240, "mime_type": "video/mp4", "chunk_size": 104857600}

201 →
{"success": true, "data": {"upload_id": "...", "chunk_size": 104857600, "total_chunks": 103,
                            "total_size": 10737418240, "expires_at": "...", "status": "initiated"}}
```

**Example — resume:** `GET /uploads/{id}` while chunks 1–50 have landed and 51–100 haven't:
```json
{"uploaded_chunks": [1,2,...,50], "missing_chunks": [51,52,...,100],
 "uploaded_bytes": 5242880000, "progress_percentage": 50.0, "status": "uploading"}
```
The client uploads exactly the `missing_chunks` — nothing else.

### Idempotency

- `POST /uploads` and `POST /uploads/{id}/complete` both accept the
  SAME `Idempotency-Key` header contract Phase 4 built for
  `/files/upload` (`IdempotencyService`, reused unchanged) — a retried
  initiate/complete with the same key replays the original response.
- `PUT /uploads/{id}/chunks/{n}` uses a *different, more specific*
  mechanism: the chunk number IS the natural idempotency key
  structurally, so re-uploading the same chunk number with IDENTICAL
  content (SHA-256 match) is a no-op (no re-upload, no duplicate GCS
  object); re-uploading with DIFFERENT content overwrites it
  (last-write-wins, serialized by a per-chunk lock). Layering the
  generic `Idempotency-Key` header on top would be redundant here.
- `POST /uploads/{id}/complete` is safe against duplicate requests
  independent of whether a key was even sent: it acquires a per-session
  lock, and if `status == COMPLETED` already, returns the existing
  file rather than erroring or redoing work.

### Concurrency control

- **Duplicate chunk records**: a real DB `UniqueConstraint(upload_id,
  chunk_number)` is the actual guarantee — `UploadChunkRepository
  .create_or_get_existing` attempts the insert inside a SAVEPOINT so a
  losing race doesn't abort the whole request's transaction. A
  per-chunk Redis lock (`upload-chunk:{id}:{n}`) makes the race rare in
  the first place; the DB constraint is what makes it SAFE even when
  the lock doesn't prevent it (e.g. TTL expiry mid-upload).
- **Incorrect uploaded byte count**: `uploaded_bytes` is never updated
  via a concurrent read-modify-write increment — a textbook
  lost-update race under parallel chunk uploads. Live progress is
  instead a fresh `SUM(size)` aggregate over VERIFIED chunks, computed
  on every read — slower per call, race-free by construction. The
  column is written exactly once, atomically, at completion.
- **Double finalization**: `complete_upload` holds a per-session Redis
  lock for its entire duration; a second concurrent call blocks, then
  observes `COMPLETED` and returns the existing result.
- **Redis unavailable**: every WRITE operation (chunk upload,
  completion, cancellation) fails closed with `503` — see
  `ChunkedUploadService._guarded_lock`, which translates an
  infrastructure failure at lock ACQUISITION into
  `ServiceUnavailableException`, while letting whatever happens *inside*
  a successfully-held lock raise its own real exception type unchanged.
  Read-only endpoints (status, chunk listing) never touch a lock and
  keep working with Redis down, since they only read Postgres.

### Checksum strategy

- **Per-chunk**: every chunk is SHA-256'd server-side on arrival,
  compared against an optional client-supplied `X-Chunk-Checksum`
  BEFORE the bytes are ever written to GCS — a corrupt chunk is
  rejected without paying a storage write.
- **Final**: GCS's Compose operation produces NO whole-object hash for
  composite objects (no MD5, only CRC32C + component count) — so
  `StorageService.compute_object_checksum` does one bounded, streamed
  read-through of the composed object (reusing the same generator
  `stream_download` already uses — constant memory) to obtain a real
  SHA-256, done exactly once per upload, at completion. This is
  compared against the client's `expected_checksum` (if supplied at
  initiate) and always recorded as `actual_checksum`.
- **Interaction with retries**: per-chunk checksums make a chunk retry
  provably safe (see Idempotency) — the server can tell "same content,
  safe no-op" from "different content, needs overwrite" without
  guessing from size alone.

### Error handling

Every Phase 6 failure mode is a domain exception subclassing an
ALREADY-REGISTERED base — `UploadSessionNotFoundException`/
`ChunkNotFoundException` (→404), `UploadSessionExpiredException`/
`UploadAlreadyFinalizedException`/`DuplicateChunkException` (→409),
`UploadIncompleteException`/`ChunkSizeInvalidException`/
`ChunkNumberInvalidException`/`ChunkChecksumMismatchException`/
`FinalChecksumMismatchException` (→400),
`InvalidUploadStateTransitionException` (→400 via the generic domain
handler). FastAPI/Starlette resolve handlers by walking the raised
type's MRO for the closest registered ancestor, so **none of these
needed a new handler function or a `main.py` change** — the same
technique `FolderNotFoundException`/`DuplicateFolderException` already
relied on since Phase 2. GCS failures reuse Phase 3's
`StorageException` hierarchy (→502/504); database failures reuse the
existing `SQLAlchemyError` handler (→503); Redis/coordination failures
map to `ServiceUnavailableException` (→503, see Concurrency Control).
Transient GCS chunk-upload failures retry via `retry_async` (Phase 4);
the Compose call at completion is instead wrapped in a `CircuitBreaker`
(cheaper to fail fast on a multi-stage compose than to retry it
wholesale) — a deliberate split of the two mechanisms across different
operations, not both stacked everywhere.

### Kubernetes behavior

The scenario from the phase brief — chunk 1 → Pod 1, chunk 2 → Pod 3,
chunk 3 → Pod 2, then Pod 2 crashes — completes correctly, because:
1. Every chunk request is a complete, independent, stateless HTTP call
   (Phase 4) — no pod holds any in-memory state about "this upload"
   between requests.
2. Each chunk's outcome (VERIFIED, with its temp object name and
   checksum) is committed to Postgres before that request returns —
   the fact "chunk 3 landed" survives Pod 2's crash intact, because it
   was never Pod 2's memory that held it.
3. `GET /uploads/{id}` on ANY surviving pod reconstructs the exact
   same missing-chunks answer Pod 2 would have given, because it reads
   the same Postgres rows.
4. Completion, whenever it happens (on whatever pod), only needs
   Postgres (chunk metadata) and GCS (chunk bytes) — never "the pod
   that handled chunk 3."

### Testing

`tests/test_chunked_upload.py` — 41 tests against the same hermetic
fakes the rest of the suite uses (SQLite, `FakeGCSClient` +
`FakeBlob.compose()`, `FakeRedisClient`): initiate, first/multiple
chunk upload, out-of-order chunk upload, safe chunk retry vs. real
overwrite, invalid chunk (size/number/checksum/oversized), missing-chunk
completion rejection, resume (partial upload → correct missing-chunks
→ complete), expiration (lazy, via direct `expires_at` manipulation),
cancellation (idempotent, temp-object cleanup, blocked once completed),
byte-exact completion + download round-trip, temp-object cleanup after
compose, duplicate/idempotent completion requests, concurrent
completion (never two files), ownership scoping (404, not 403, for a
non-owned session), invalid state transitions, simulated database/GCS/
Redis failures (503/502/503 respectively, never a raw leaked
exception), large declared file size, invalid file size, chunk-count
ceiling. A dedicated `CHUNK_MIN_SIZE_BYTES=1024` test-only override
(`tests/conftest.py`, same pattern as the existing `RETRY_*` overrides)
keeps the suite fast without weakening what's actually exercised.

### Load testing

`scripts/load-test/` — see its own `README.md` for full instructions,
metrics to watch, and explicit "what NOT to conclude" caveats. Summary:
a k6 script (recommended) and an equivalent Locust script both simulate
100 concurrent users each running the full initiate → parallel chunk
upload → (sometimes) simulated-drop-and-resume → complete →
download-verify lifecycle, with configurable deliberate chunk
corruption to exercise retry paths.

### Design decisions

- **Temp-object-per-chunk + Compose over a single GCS resumable
  session** — the only GCS-native option that supports genuine
  parallel chunk upload; see "GCS upload architecture" above for the
  full reasoning and the corruption risk that rules out the
  alternative.
- **Chunk bytes still transit FastAPI** (not a client-direct-to-GCS
  handoff) — dictated by this phase's own `PUT /uploads/{id}/chunks/{n}`
  endpoint contract; the memory-safety requirement is satisfied by
  never buffering more than one bounded chunk, not by bypassing the
  app entirely.
- **`AuditMixin`, not `SoftDeleteMixin`, on `UploadSession`** — an
  abandoned/expired session isn't "trash" a user restores; its own
  `status` enum already captures that lifecycle more precisely.
  `UploadChunk` gets neither mixin — an immutable, short-lived,
  high-write fact needs no audit/soft-delete semantics beyond its FK.
- **Lazy expiration, not a background sweeper** — checked on every
  access (`_apply_expiration_if_needed`), exactly as the phase brief
  specifies ("design cleanup so it can later be handled by a background
  worker" — background workers are explicitly out of scope this phase).
- **Progress computed by live aggregate query, never a maintained
  counter** — the direct fix for the "incorrect uploaded byte count
  under concurrency" requirement; see Concurrency Control.
- **No content-dedup extension to the chunked path** — Phase 3's
  checksum-based whole-file dedup (`FileMetadataRepository
  .get_by_checksum`) is deliberately NOT applied to freshly-composed
  chunked-upload objects this phase, to keep the already-large surface
  area testable; `actual_checksum` is still computed and stored,
  leaving this as a clean, low-risk future addition rather than an
  untested one shipped speculatively.
- **`retry_async` for chunk uploads, `CircuitBreaker` for Compose** —
  different failure shapes: a single chunk PUT is cheap to retry a
  couple of times; a multi-stage Compose across dozens of parts is not
  — failing fast once GCS is clearly unhealthy is the better fit there.

### Performance analysis

- **CPU**: SHA-256 hashing (per chunk, and once more for the final
  composed object) is the main CPU cost — bounded per chunk by
  `CHUNK_MAX_SIZE_BYTES`, and the final hash is one streamed pass, not
  proportional to chunk count.
- **Memory**: bounded to one chunk's worth at a time (`_read_body_bounded`
  enforces this at the ASGI-stream level, not just via a
  `Content-Length` check) — a Pod handling a 100 GB chunked upload
  never holds more than `CHUNK_MAX_SIZE_BYTES` (default 256 MiB
  ceiling) of file content in memory at once, regardless of total file
  size.
- **Network**: chunk bytes traverse client → Pod → GCS once each (no
  double-hop); the Compose step is GCS-internal (server-side
  concatenation), so reassembly does NOT re-transfer chunk bytes over
  the network a second time — only the one checksum-verification
  read-through at completion does.
- **Database**: one row read + one row write per chunk (light) — see
  the "Transaction boundaries" note in `ChunkedUploadService`'s module
  docstring: no transaction is ever held open across a slow GCS call.
  Connection pool math is the same as Phase 5's documented ceiling
  (`app/database/session.py`) — chunked uploads don't change it, since
  each chunk request is its own short-lived connection checkout.
- **GCS**: N parallel chunk PUTs = N concurrent GCS write requests;
  Compose is capped at 32 sources per call (recursed for more), so
  completion cost scales as `O(log₃₂(total_chunks))` GCS calls, not
  `O(total_chunks)`.
- **Concurrency**: parallel chunk upload throughput is bounded by GCS's
  own per-object/per-bucket write throughput and the Pod's own HPA
  ceiling (Phase 5), not by any NimbusFS-side global lock — the only
  locks are scoped per-chunk or per-session, never upload-wide.

### Failure scenarios

| Scenario | What happens |
|---|---|
| Pod crashes mid-chunk-upload | That one HTTP request fails (client sees a connection error); no state is corrupted (the chunk row either committed or didn't — no partial row); client retries the SAME chunk number against any surviving pod |
| GCS unavailable during a chunk PUT | `retry_async` retries transiently, then `502`/`504` (translated `StorageException`) if still down; chunk row is never created for a failed upload — the chunk_number stays free for retry |
| GCS unavailable during Compose | `CircuitBreaker` opens after repeated failures (fast-fail on subsequent attempts); session transitions to `FAILED` (not stuck in `COMPLETING`); client retries `POST .../complete` once GCS recovers |
| Database unavailable | Any DB-touching call surfaces `503` via the existing `SQLAlchemyError` handler (Phase 1) |
| Network disconnects mid-upload | Client simply resumes later — `GET /uploads/{id}` reports exactly what's missing, from any pod, at any time up to `expires_at` |
| A chunk fails validation (wrong size/checksum) | Rejected with `400` BEFORE any GCS write — no wasted storage cost, chunk_number stays free for a corrected retry |
| Client retries a chunk it already sent | Detected as identical (checksum match) → no-op `200`, no duplicate object, no duplicate row |
| Completion request repeated | Detected via `status == COMPLETED` → same result returned, no re-compose, no duplicate `FileMetadata` |

### Interview questions

**Beginner**
- *Why can't you just PUT the whole file in one request for a 50 GB file?* — Network reliability over minutes-long single requests is poor, there's no way to parallelize, and any failure loses all progress; chunking bounds each request's blast radius and enables resuming from exactly where it left off.
- *What does "resumable" actually require the server to track?* — Which pieces (chunks) of the file have durably landed, so a client reconnecting can ask "what's left?" instead of restarting.

**Intermediate**
- *Why is a single GCS resumable session not enough for "parallel chunk upload"?* — It's a single-writer, single-offset-cursor stream; concurrent writes to it race and corrupt the upload. Independent temp objects + Compose is the GCS-native way to get true parallelism.
- *How do you prevent two concurrent requests from both writing chunk 5?* — A DB unique constraint on `(upload_id, chunk_number)` is the ultimate guarantee (via a SAVEPOINT-guarded insert so a losing race doesn't abort the whole transaction), backed by a per-chunk Redis lock that makes the race rare in the first place.
- *Why not increment `uploaded_bytes` as each chunk lands?* — Concurrent read-modify-write increments from parallel chunk uploads is a classic lost-update race; computing progress via a live `SUM()` aggregate is race-free by construction, at the cost of a slightly more expensive read.

**Advanced**
- *Walk through what happens if the pod handling `POST .../complete` crashes mid-Compose.* — The session is left in `COMPLETING` (not `COMPLETED`) since the status flip to `COMPLETED` only happens after Compose, checksum verification, and `FileMetadata` creation all succeed. A later `complete` call re-acquires the session lock, observes non-terminal `COMPLETING` state... — and this is worth being honest about as a real edge case: `COMPLETING` is not in `UploadStateMachine`'s valid-transition SOURCE set for re-entry to `COMPLETING` itself (only `UPLOADING`/`FAILED` can transition TO `COMPLETING`), so a genuinely stuck `COMPLETING` session (crash mid-compose, no exception ever reached the `except Exception: status = FAILED` handler because the PROCESS died, not just the request) requires operator intervention or a future phase's background reconciliation job to detect and reset — a real, acknowledged limitation of "no background workers this phase," not a silently swept-under-the-rug one.
- *How would you extend this to bypass FastAPI entirely for chunk bytes?* — Swap the chunk-PUT handler for one that calls `Blob.create_resumable_upload_session()` (or issues a per-chunk signed URL) and hands the URI/URL to the client instead of accepting bytes directly — the `UploadSession`/`UploadChunk` schema and state machine don't need to change, only how a chunk's bytes physically travel; the completion/compose logic is unaffected either way.

### Phase 6 completion checklist

- [x] Upload session creation, chunking math (server-computed
      `total_chunks`, last-chunk-size handling)
- [x] Chunk upload, tracking, resume, missing-chunk detection
- [x] Parallel chunk upload support (independent temp objects, no
      shared cursor)
- [x] Chunk retry (checksum-based safe no-op vs. overwrite)
- [x] Per-chunk and final checksums (SHA-256, verified before/after storage writes)
- [x] Upload progress (live aggregate, race-free under concurrency)
- [x] Upload completion (Compose, multi-stage for >32 chunks, final
      checksum, FileMetadata/FileVersion creation, temp cleanup)
- [x] Upload cancellation (idempotent, blocks `COMPLETED → CANCELLED`)
- [x] Upload expiration (lazy, on-access — no background sweeper this phase)
- [x] Explicit upload state machine, centralized, not scattered in routes
- [x] Idempotency (`Idempotency-Key` for initiate/complete; checksum-based for chunks)
- [x] Concurrent-upload protection (DB unique constraint + SAVEPOINT, per-chunk/per-session Redis locks)
- [x] Failure recovery (retry for chunks, circuit breaker for Compose, FAILED→retry state transition)
- [x] GCS integration (temp objects + Compose, no invented storage mechanism)
- [x] Kubernetes/multi-pod compatibility (Postgres-authoritative state, verified via the failure-scenario table above)
- [x] Full REST API, RESTful status codes, OpenAPI-documented
- [x] Database migration (`0004_chunked_uploads...`), reversible
- [x] 41 tests (unit + integration), hermetic fakes only
- [x] k6 + Locust load tests, with documented metrics and "what NOT to conclude" caveats
- [x] README + this design-decision writeup
- [ ] Pub/Sub, background workers, full Redis caching, disaster
      recovery, multi-region storage, CI/CD, full observability stack,
      AI features, a real virus-scanning service, advanced enterprise
      security, advanced deduplication — explicitly out of scope,
      deferred to later phases

## 14. Distributed Redis Caching & Coordination *(Phase 7)*

Phase 4 built the Redis *plumbing* (a pool, distributed locks,
idempotency-key storage, health checks) and deliberately stopped there.
Phase 7 makes Redis a real distributed **caching and coordination layer**:
cache-aside reads with stampede protection, centralized invalidation, a
production distributed-lock facade, and real rate limiting replacing the
Phase 4 no-op placeholder.

The full engineering rationale — race analysis, failure catalogue,
interview Q&A — lives in **[`docs/PHASE_7_REDIS_DESIGN.md`](docs/PHASE_7_REDIS_DESIGN.md)**.
This section is the walkthrough.

### 14.1 The invariant everything follows from

> **PostgreSQL owns metadata. GCS owns bytes. Redis owns nothing.**
> Flushing Redis entirely, at any moment, must cost only latency.

```
   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
   │  Pod 1       │   │  Pod 2       │   │  Pod N       │   stateless replicas
   │ CacheService │   │ CacheService │   │ CacheService │   (Phase 4/5)
   │ RateLimiter  │   │ RateLimiter  │   │ RateLimiter  │
   │ DistLock     │   │ DistLock     │   │ DistLock     │
   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
          └──────────────────┼──────────────────┘
                             ▼
          ┌──────────────────────────────────────────┐
          │  REDIS / Memorystore  —  shared, ephemeral│
          │  cache · locks · rate-limit buckets       │
          │  *** never authoritative, never bytes *** │
          └──────────────────┬───────────────────────┘
                     miss    │
                             ▼
          ┌──────────────────────────────────────────┐
          │  PostgreSQL — AUTHORITATIVE for metadata  │
          └──────────────────────────────────────────┘
```

Two consequences enforced in code, not just documented:

1. `CacheSerializer.encode` **raises** if handed `bytes`. File content
   physically cannot be written to Redis by this codebase.
2. Every `CacheService` method catches every Redis exception, **logs it**,
   and returns the "as if the cache did not exist" answer. A cache failure
   degrades performance; it can never fail a request. There is a test that
   kills Redis mid-suite and asserts the API keeps answering.

### 14.2 New modules

```
app/core/cache/
  keys.py             CacheKeyBuilder   — WHAT a key is called
  serializer.py       CacheSerializer   — HOW a value is encoded
  policy.py           CachePolicy       — HOW LONG a value lives
app/core/
  rate_limiter.py     RateLimiter       — token bucket in atomic Lua
  distributed_lock.py + DistributedLockService  (extends Phase 4)
app/services/
  cache_service.py    CacheService      — the ONLY gateway to Redis-as-cache
  cache_invalidator.py CacheInvalidator — operation → key-set fan-out
app/dependencies/
  rate_limit.py       rate_limit(category) dependency + provider
app/middleware/
  rate_limit.py       RateLimitHeadersMiddleware (was the Phase 4 no-op)
```

`redis.asyncio` is imported by exactly three modules: the pool
(`app/database/redis.py`), `CacheService`, and `RateLimiter`. No route
handler and no other service talks to Redis directly — the single most
important structural rule of this phase, because scattered Redis calls are
how a *cache* outage becomes an *application* outage.

### 14.3 Cache-aside, and why not write-through

```
   READ                                   WRITE
   GET key ──hit──► return                UPDATE row in Postgres
      │                                          │
     miss                                        ▼
      ▼                                   DELETE key (+ related)
   SELECT from Postgres                   ── never "UPDATE key"
      ▼
   SET key with TTL ──► return
```

| Strategy | Why not |
|---|---|
| Write-through | Every write pays cache latency for data often never re-read — and it is a stale-data source under concurrency: two writers can apply *cache* updates in the opposite order to their *database* commits, leaving the cache permanently wrong with no TTL-independent way to notice. |
| Write-behind | Makes Redis authoritative for a window. Violates the invariant. |
| Read-through | Puts SQL behind the caching abstraction, inverting the dependency. |
| **Cache-aside** ✅ | The cache is purely opportunistic. Empty, partial, or absent — the system is still correct. |

Invalidation always **deletes**, never updates: delete is idempotent and
order-independent, so the loser of any race just causes one extra read.

### 14.4 Cache key strategy

```
nimbusfs : <entity> : <id> [ : <derived> ] [ : <fingerprint> ]
```

| Key | Entity |
|---|---|
| `nimbusfs:user:{user_id}` | user profile |
| `nimbusfs:folder:{folder_id}` | folder metadata |
| `nimbusfs:folder:{folder_id}:children:{fp}` | children listing, per sort/filter variant |
| `nimbusfs:folder:root:{owner_id}:children:{fp}` | top-level listing |
| `nimbusfs:folder:{folder_id}:breadcrumbs` | breadcrumb trail |
| `nimbusfs:file:{file_id}` | file metadata |
| `nimbusfs:file:{file_id}:versions` | version history |
| `nimbusfs:search:{owner_id}:{fp}` | one search result page |
| `nimbusfs:ratelimit:{category}:{identity}` | token bucket |

Collision safety: the entity type is always the second segment, so
`folder:<uuid>` and `file:<uuid>` cannot alias. Variable-length or
user-supplied components (search filters, listing sort params) are never
interpolated raw — they are canonicalized to sorted `k=v` pairs and
SHA-256'd, which keeps keys fixed-length and removes any chance of a value
containing `:` and forging a different key shape. `None` encodes as
`~none`, never `"None"`, so a folder literally named `None` cannot alias
the root.

**Authorization is never cached.** Resources are. Folder/file/user entries
are keyed *by resource*, and the cached payload carries `owner_id`; the
service re-applies exactly the ownership + not-deleted filter the
repository's `WHERE` clause would have applied, raising the same **404**
(never a 403 — IDs must stay unguessable). Search is the one entity where
per-caller keying is correct rather than a pessimization, because a result
set has no single owner field to re-check — so its key is caller-scoped.
There is a test where user B tries to read a folder user A just warmed
into the cache and gets a 404.

### 14.5 TTL strategy

| Entity | Default | Reasoning |
|---|---|---|
| user | 900s | Changes are rare and administrative. **Not on the auth path** — `get_current_user` still reads Postgres every request so deactivation stays immediate (Phase 1's decision, deliberately preserved). |
| folder | 300s | Explicit invalidation is the mechanism; TTL is the backstop. |
| folder children | 300s | Highest-churn folder key — also the most invalidation call sites. |
| folder breadcrumbs | 300s | Changes only on an *ancestor* rename/move. |
| file | 300s | Mirrors folder metadata. |
| file versions | 300s | Append-only in practice. |
| search | 90s | A derived view over many rows that cannot be invalidated precisely. Shortest by design. |

TTL is a **correctness** knob wearing a performance costume: it is the hard
ceiling on staleness if invalidation is ever missed, dropped, or raced.
Every value comes from `Settings.CACHE_TTL_*` via `CachePolicy` — nothing
is hardcoded at a call site.

### 14.6 Invalidation strategy

| Operation | Keys cleared |
|---|---|
| folder create / rename / trash / restore / purge | `folder:{id}*`, parent's `children:*` |
| folder move | `folder:{id}*`, **old** parent's `children:*`, **new** parent's `children:*` |
| folder trash/restore of a subtree | the above **for every descendant individually** (each has its own `is_deleted` flag cached) |
| file create / update / rename / trash / restore / purge / new version | `file:{id}*`, folder's `children:*`, `search:{owner}:*` |
| file move | the above, plus the destination folder's `children:*` |

Pattern deletes use `SCAN`, **never `KEYS`** — `KEYS` is O(N) over the
whole keyspace and blocks Redis's single command thread, which on a
production instance is a self-inflicted outage.

**The race we accept, stated plainly.** Invalidation runs inside the
request's transaction (`get_db` commits at the request boundary), so
between a writer's `DEL` and its `COMMIT` a concurrent reader can miss,
read the still-old committed row, and write it back. Bounded three ways:
the window is the remainder of one transaction (sub-millisecond in
practice); staleness is capped at the entity TTL, not unbounded; and
`CACHE_WRITE_GUARD_SECONDS > 0` (**1.5s, ON by default**) closes it
entirely with a post-invalidation tombstone that rejects the stale write —
implemented and tested; set to `0.0` to disable if the extra round trip
matters more than the race for a given deployment. The airtight fix —
invalidating in a SQLAlchemy `after_commit` hook — still needs a
transaction-lifecycle hook the current per-request Unit of Work does not
expose to services, so the guard is the pragmatic close, not the
architectural one. Documented as a real limitation, not hidden.

**Descendant breadcrumbs (precisely invalidated, not just bounded by
TTL):** renaming or moving a folder changes every descendant's
materialized `path`, and therefore every descendant's breadcrumb cache.
`FolderRepository.list_descendants()` already existed (soft-delete cascade
uses it), so `rename_folder`/`move_folder` capture the descendant ID list
*before* `cascade_rename` rewrites paths (IDs are stable across the
rewrite) and pass it to `CacheInvalidator.descendant_breadcrumbs_changed()`,
which deletes each descendant's exact `breadcrumbs` key. No SCAN, no new
Redis index — O(descendant count) exact deletes, the same shape
`cascade_rename` already pays in Postgres for the same operation.

### 14.7 Stampede protection (single-flight with a bounded wait)

When a hot key expires under load, plain cache-aside makes *every*
concurrent request miss simultaneously — one query becomes hundreds
against an already-busy database. That is a cache stampede.

```
 request ──► GET key ──hit──► return          (hot path: no locking at all)
               │
              miss
               ▼
       SET NX  nimbusfs:lock:cache:{hash}  TTL 5s
               │
      ┌────────┴─────────┐
   won│                  │lost
      ▼                  ▼
  re-GET (double-check)  poll GET every 20ms, up to 500ms
      │ hit ► return      │
     miss             ┌───┴────┐
      ▼          published    timeout
  SELECT + SET       │          │
      ▼              ▼          ▼
  release, return  return   SELECT from Postgres  ◄─ read through; never block forever
```

Four properties worth naming:

- The **double-check after winning** is what makes this correct rather
  than merely lucky — someone may have published while we acquired.
- The **follower fallthrough is the important choice.** No request ever
  blocks indefinitely on another request's work. Unbounded waiting turns
  one slow query into worker-pool exhaustion and then a total outage —
  strictly worse than the stampede it prevents. The guarantee is
  deliberately *"far fewer DB hits than requests"*, not *"exactly one"*.
- A **crashed winner self-heals** via the 5s lock TTL.
- **Coordination failure is non-fatal**: if Redis errors during lock
  acquisition, it is logged and everyone degrades to plain cache-aside.
  The lock is a performance optimization here, not a correctness mechanism.

Tested with 50 concurrent requests for one uncached key: fewer than 10 may
reach the source, and every caller must still get a correct answer.

### 14.8 Distributed locking

The algorithm is Phase 4's, unchanged, because it was already correct:

```
ACQUIRE:  SET lock:<key> <uuid4-token> NX PX <ttl_ms>     (atomic claim-or-fail)
RELEASE:  EVAL  if redis.call("get",KEYS[1]) == ARGV[1]
                then return redis.call("del",KEYS[1]) else return 0 end
```

The Lua-guarded release prevents the classic **lost-lock** bug: if A's work
outlives its TTL, the lock expires, B acquires it, and A's later `DEL`
would delete *B's* lock. The token check makes that impossible — and it
must be atomic, which is why it is a script and not `GET` then `DEL`.

Phase 7 adds around that unchanged core: `acquire_with_timeout()` (bounded,
**jittered** retry — never "wait forever"), `owns()` (authoritative
ownership check vs. `is_held`'s local belief), `release(strict=True)`
(raises `LockOwnershipError` instead of silently no-op'ing), and
`DistributedLockService`, a facade whose real value is refusing to conflate
two failure modes:

| Failure | Meaning | Outcome |
|---|---|---|
| Contention | Someone else holds it | `LockAcquisitionTimeout` → 409 (existing Phase 4 handler) |
| Redis down at **acquire** | Exclusivity cannot be proven | `DistributedLockError` — **never** "the lock is free" |
| Redis down at **release** | Work already happened; TTL frees it | Logged and swallowed |

TTL-based expiry, **not** renewal/Redlock: a crashed holder blocks others
for at most `ttl_seconds`. Redlock is deliberately out of scope — it is
contested in the literature, and NimbusFS's locks never carry the *final*
correctness guarantee. Real guarantees live in Postgres constraints (Phase
6's `UniqueConstraint(upload_id, chunk_number)` is what actually prevents
duplicate chunks; the lock only makes the race rare).

### 14.9 Rate limiting — algorithm choice

| Algorithm | Verdict |
|---|---|
| Fixed window counter | Cheapest, and wrong at the boundary: full budget at the end of one window plus full budget at the start of the next = **2x** the intended rate in a sub-second span. On a login endpoint that doubling lands exactly where it hurts. |
| Sliding window log | Exact, but memory linear in request rate — one sorted-set member per request per window, plus O(log N) trims. |
| Sliding window counter | Bounded memory, no boundary doubling, but an *approximation* that cannot express burst separately from sustained rate. |
| **Token bucket** ✅ | Two numbers per key: O(1) memory and time. Burst (`capacity`) and sustained rate (`capacity/window`) are separate tunables. And it yields an **exact** `Retry-After` from the token deficit, where the counter approaches can only guess — a client told precisely when to return does not poll. |

```
capacity ┤ ████████████                     ████████
         │ ████████████                 ████████████
 tokens  │ ██████                 ██████████████████
       0 ┼─────┬──────────────────┬───────────────────► time
           burst drains it        refill at N/W per sec
                                  (capped — no banking)
```

Executed as **one atomic Lua script**: read-modify-write from N pods is a
lost-update race (two replicas both read "1 token left", both allow).
Redis runs Lua atomically on its single command thread, making
refill→check→decrement indivisible. `WATCH`/`MULTI`/`EXEC` would need
optimistic-retry loops that peak exactly when the limiter is hottest.

**Independent budgets per category** so exhausting search cannot starve an
in-flight upload:

| Category | Default | Applied to |
|---|---|---|
| `login` | 10 / 60s | `POST /auth/login` |
| `register` | 5 / 300s | `POST /auth/register` |
| `metadata` | 300 / 60s | `/folders/*` and `/metadata/*` (router-level) |
| `search` | 60 / 60s | `GET /metadata/search` (stacked on top of `metadata`) |
| `upload_initiate` | 60 / 60s | `POST /uploads` |
| `upload_complete` | 60 / 60s | `POST /uploads/{id}/complete` |
| `default` | 600 / 60s | fallback |

Per-**chunk** `PUT /uploads/{id}/chunks/{n}` is deliberately **not**
limited: one large upload legitimately issues thousands of parallel chunk
PUTs (the entire point of Phase 6), so a per-request budget there would
throttle correct behavior rather than abuse.

**Why a dependency, not middleware.** Middleware sees only a method and a
path string, so classification means a path-pattern table that rots the
moment a route is renamed. A dependency lives next to the endpoint, moves
with it, shows up in OpenAPI, runs before the handler body (a rejected
request costs no DB/GCS work), and is overridable in tests. `/folders` and
`/metadata` apply the budget at **router** level so a newly-added route
cannot silently be unprotected.

**Identity:** the JWT `sub` claim when a valid Bearer token is present
(decoded locally, signature-verified, **no DB round trip**), else the
client IP from `TrustedProxyMiddleware`. An invalid token is limited by IP
— correct, since an attacker brute-forcing tokens has no identity. **No
authorization happens here**: the token is an identity hint for bucketing
only; every route still runs `CurrentUser` and its own ownership checks.

**The 429 contract:**

```
HTTP/1.1 429 Too Many Requests
Retry-After: 6
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 0
X-RateLimit-Category: login

{"success": false, "message": "Rate limit exceeded for 'login': ...",
 "data": null, "errors": null, "timestamp": "...", "request_id": "..."}
```

Successful responses carry the same `X-RateLimit-*` headers, and unlimited
routes still report `unlimited` rather than omitting them — so the Phase 4
placeholder's client contract is *honored*, not broken, now that limits are
real. That was the entire point of shipping the placeholder.

### 14.10 Failure-handling / degradation matrix

| Scenario | Behavior | User impact |
|---|---|---|
| Redis crashes / unreachable | Every cache op catches, logs `cache_error`, returns miss/no-op; reads fall through to Postgres; rate limiter fails open; locks at acquire raise `DistributedLockError` | **None functionally.** Higher latency, higher DB load |
| Redis slow (not down) | `socket_timeout=2s` converts slow into an error → same path. The tight timeout is the point: a *slow* cache is worse than an *absent* one | ≤ +2s on the first affected command |
| Connection pool exhausted | Treated exactly like an outage | As above. Sizing: `REDIS_MAX_CONNECTIONS` (20) × replicas; HPA max 10 → 200 against Memorystore |
| Cache stale | Bounded by explicit invalidation + per-entity TTL + optional write guard | ≤ TTL of wrong data, worst case |
| Stampede lock expires mid-populate | Another request becomes the winner; both write the same DB-derived value | None — a duplicate query, not a correctness problem |
| Lock owner crashes | TTL expiry frees it; no shutdown hook required (`kill -9` never runs one) | Others wait ≤ lock TTL |
| Rate limiter unreachable | Fail-open (default): allow + log at ERROR. Fail-closed: 429. Both implemented and tested | Fail-open: none. Fail-closed: total 429 |
| Multiple pods, same key, same instant | Reads collapse via single-flight; writes are last-writer-wins on identical values; `DEL` is idempotent; rate-limit buckets are ONE atomic bucket shared by all pods | None |
| Rolling deploy, two schema versions live | Envelope `v` mismatch reads as a **miss**; both builds repopulate in their own format | One cold period, never a crash |
| Oversized value | Logged `cache_skipped_too_large`, write skipped, request succeeds from Postgres | None |
| Redis memory pressure | Early eviction is indistinguishable from TTL lapse. **Ops:** set `maxmemory-policy allkeys-lru`; `noeviction` would turn a full cache into write errors | None |

### 14.11 Local dev vs production Memorystore

| | Local (`docker-compose.yml`) | Production |
|---|---|---|
| Instance | `redis:7-alpine`, no auth | Cloud Memorystore, **Standard tier** (HA + automatic failover) |
| Address | `REDIS_HOST=redis` | Private Service Access IP (e.g. `10.0.0.4`), never public |
| Auth / TLS | none | AUTH string in the Secret; in-transit encryption on |
| Persistence | none needed | **none needed** — Redis holds nothing that matters |
| Eviction | default | `allkeys-lru` |
| Failure drill | `docker compose stop redis`, watch the API keep serving | The same behavior, exercised by a real failover |

### 14.12 Kubernetes / GKE compatibility

Nothing in Phase 7 weakens the statelessness Phase 4 established or the
manifests Phase 5 wrote:

- **No pod-local cache, no sticky sessions.** All cache state lives in
  Memorystore, shared identically by every replica, so a request may land
  on any pod. Scaling 3→10 or evicting a pod changes nothing.
- **Rate limits are genuinely global.** Buckets are shared, so the
  effective limit is per-user, not per-user-per-pod. An in-process limiter
  would have silently multiplied every budget by the replica count — the
  reason `slowapi` and friends are unusable here.
- **Locks are TTL-based**, so a `SIGKILL`ed pod self-heals with no
  shutdown hook — matching the PodDisruptionBudget/rolling-update model.
- **Only one manifest changed**: additive keys in `k8s/05-configmap.yaml`,
  consumed by the existing `envFrom`, so `07-deployment.yaml` was not
  touched. `11-networkpolicy.yaml` already allows egress to the
  Memorystore CIDR — still a placeholder that must be replaced with the
  real range before a real deploy (unchanged Phase 5 caveat).

### 14.13 Observability

Structured `structlog` events only — a full Prometheus/OpenTelemetry stack
is explicitly out of scope. The events are written to be *trivially*
scrapable into metrics later: stable names, flat key/value pairs, numeric
`duration_ms` on everything.

`cache_hit` · `cache_miss` · `cache_set` · `cache_delete` ·
`cache_invalidated` · `cache_error` · `cache_skipped_too_large` ·
`cache_stampede_leader` / `_follower_served` / `_follower_read_through` ·
`lock_acquired` / `lock_contended` / `lock_acquire_timeout` /
`lock_released` / `lock_release_not_owned` / `lock_redis_error` ·
`rate_limit_allowed` / `rate_limit_rejected` / `rate_limit_degraded`

`request_id` / `correlation_id` / `trace_id` / `server_id` are never passed
explicitly — `RequestContextMiddleware` binds them into structlog
contextvars for the whole request, so they land on every line above
automatically, including across `await` boundaries.

**Rate-limit identities are hashed, never logged raw** (they are a user ID
or a client IP — personal data with no business in a log aggregator). The
hash is stable, so "this caller keeps getting limited" stays answerable.

**Silence and fallback are different things.** Every degradation path logs
before it degrades. A cache failing silently for a week while the app
serves from Postgres at higher latency is a far worse incident than a loud
one.

### 14.14 Performance testing

`scripts/benchmark/benchmark_cache.py` measures with-cache vs
without-cache latency (p50/p90/p99) for three read paths:
`GET /folders/{id}` (the *floor* — how much is pure network/serialization),
`GET /folders/breadcrumb` (one query **per ancestor** uncached — the
largest expected win), and `GET /metadata/search` (query + COUNT).

```bash
pip install httpx

# Arm 1 — cache ON
CACHE_ENABLED=true RATE_LIMIT_ENABLED=false uvicorn app.main:app --port 8000
python scripts/benchmark/benchmark_cache.py --label cached --out cached.json

# Arm 2 — restart with CACHE_ENABLED=false, then
python scripts/benchmark/benchmark_cache.py --label uncached --out uncached.json

python scripts/benchmark/benchmark_cache.py --compare cached.json uncached.json
```

`CACHE_ENABLED` is read once per process (`get_settings()` is `lru_cache`d),
so the two arms need two server starts — deliberate, since a benchmark that
flipped the flag mid-run would be measuring a half-warm process.

> **No benchmark numbers are published anywhere in this repository.** A
> speedup figure measured on someone else's laptop against a different
> Postgres with a different working-set size is worse than no figure at
> all. See `scripts/benchmark/README.md` for the runbook and, more
> importantly, for what **not** to conclude — chiefly that this measures
> latency at concurrency 1, while a cache's primary job is shedding load
> under concurrency.

### 14.15 Phase 7 Design Decisions

- **JSON, never pickle**, for cache values. Three independent
  disqualifiers: `pickle.loads` on a shared, network-reachable,
  multi-writer datastore is arbitrary code execution; pickle encodes class
  paths so a routine rename breaks every entry *mid-rolling-deploy*; and
  pickle is unreadable from `redis-cli` during an incident.
- **Every cached value carries a schema version**, and an unrecognized
  version is treated as a **cache miss**, not an error. This is what makes
  a cache-format change safe to deploy: during a rolling deploy both builds
  read the same Redis, and the worst outcome is one cold period instead of
  a partial outage.
- **Postgres stays authoritative, and the code enforces it** —
  `CacheSerializer.encode` raises on `bytes`, and every Redis exception is
  caught, logged, and degraded to a miss. Cache failures cost latency,
  never correctness or availability.
- **One Redis pool, not two.** Phase 7 reuses Phase 4's
  `app/database/redis.py` pool rather than creating a cache-specific one:
  one place to size, one place to observe, one bounded connection ceiling
  against Memorystore.
- **All Redis access funnels through `CacheService`/`RateLimiter`.**
  Scattered `await redis.get(...)` calls are how a cache outage becomes an
  application outage — every call site would have to independently get
  error handling right, and they never all do.
- **Delete on write, never update.** Delete is idempotent and
  order-independent; write-through cache updates can be applied in the
  opposite order to their database commits, leaving the cache permanently
  wrong with no TTL-independent way to detect it.
- **Single-flight stampede protection with a bounded follower wait**, and
  an explicit *"far fewer DB hits than requests"* guarantee rather than
  *"exactly one"*. Unbounded waiting converts one slow query into
  worker-pool exhaustion — strictly worse than the stampede.
- **Resource-scoped cache keys plus an ownership re-check**, not
  caller-scoped keys, for folders/files/users — except search, where a
  result set has no owner field to re-check, so caller-scoping is the
  correct answer. Authorization is re-derived on every cached read; a
  decision is never cached. A non-owner gets a **404**, not a 403, so IDs
  stay unguessable.
- **The user cache is deliberately not on the auth path.**
  `get_current_user` still reads Postgres on every request, preserving
  Phase 1's "deactivation takes effect immediately" property. Caching the
  profile endpoint is safe; caching the authorization lookup would have
  silently undone a security decision.
- **Token bucket over sliding window**, in one atomic Lua script: O(1)
  memory and time, burst and sustained rate as separate tunables, and an
  exact `Retry-After` from the token deficit rather than a guess.
- **Rate limiting as a route dependency, not middleware** — the budget
  lives next to the endpoint it governs, moves with it, and appears in
  OpenAPI, instead of in a path-pattern table that rots on the next rename.
- **Fail-open by default on rate limiting**, loudly logged and
  configurable. This is abuse mitigation behind GCLB, not an authorization
  control; failing closed would turn a Redis blip into a fleet-wide 429
  storm for users mid-upload. `RATE_LIMIT_FAIL_OPEN=false` is implemented
  and tested for deployments where the trade-off inverts.
- **`SCAN`, never `KEYS`**, for pattern invalidation — `KEYS` is O(N) over
  the whole keyspace and blocks Redis's single command thread.
- **`DistributedLockService` is a facade, not a second lock
  implementation.** Phase 4's `SET NX PX` + Lua-checked release was already
  correct; what was missing was centralized timeout policy, ownership
  introspection, and — most importantly — refusing to conflate *contention*
  (409) with *Redis unreachable* (never "the lock is free").
- **Exactly one new exception handler.** `RateLimitExceeded` needs a 429
  with `Retry-After` that no existing handler can produce; every other new
  Phase 7 exception subclasses an already-registered base and is mapped
  correctly for free via FastAPI's MRO walk — the technique Phase 6
  established.
- **Search caching is deliberately the most conservative**: shortest TTL
  (90s), a hard row-count ceiling, a byte ceiling, and coarse per-user
  invalidation on every file write. A result set is a derived view that
  cannot be invalidated precisely without a reverse index from row to
  query — a search-engine feature, not a cache feature.
- **Known, acknowledged gaps** (documented rather than hidden): invalidation
  is not post-commit (§14.6's race), narrowed but not eliminated by the
  now-default-on `CACHE_WRITE_GUARD_SECONDS` tombstone; no negative caching
  (a hot 404 hits Postgres every time — deliberate, since caching it would
  make a just-created resource 404 for a full TTL); no probabilistic early
  expiration; no cache warming and no metrics backend. Descendant
  breadcrumb staleness on ancestor rename, previously in this list, is
  fixed — see §14.6.
- **Out of scope this phase, unchanged:** Pub/Sub, background workers,
  disaster recovery, multi-region, a Prometheus/OpenTelemetry stack, CI/CD,
  AI features, virus scanning, advanced dedup.

### 14.16 Testing

`tests/test_caching.py` (72 tests) and `tests/test_rate_limiting.py` (29
tests) — **101 new tests, 246/246 passing, zero regressions** against the
pre-existing 145.

Neither touches a real Redis. `tests/fakes/fake_redis.py` was **extended**
(not replaced) with hash storage, `SCAN`, `INCR`, `EXPIRE`, `TTL`, real
token-bucket arithmetic mirroring the Lua, a **controllable clock**
(`FakeClock` — so "the lock expires after 30s" and "the bucket refills
after the window" are instant, deterministic assertions rather than
`sleep`s), and **failure injection** (`start_failing(*commands, after=N)`)
— which is what makes every degradation assertion genuine rather than
aspirational.

Coverage: key naming/collision-safety/fingerprint stability · serializer
round-tripping, schema versioning, bytes refusal · get/set/delete/exists/
expire/increment/TTL/size limits/disabled switch · cache-aside population
and the 50-concurrent-request stampede assertion · lock release on loader
exception · Redis failure degradation on *every* operation · invalidator
fan-out per operation and per-user search scoping · the opt-in write guard
· lock acquisition, contention, expiry, ownership, the lost-lock
Lua-release guard, strict release, timeout, and "Redis down is never
mistaken for a free lock" · rate limits within/over budget, exact
`Retry-After`, refill over controlled time, capacity capping, 20-concurrent
single-bucket enforcement, identity and category isolation, reset, peek,
fail-open and fail-closed, forged-token IP fallback · and end-to-end
cache/DB consistency through the real HTTP API across
create/read/rename/move/trash/restore/delete/version/search, plus the
cross-user 404 authorization test and a full "Redis is dead, the API keeps
working" test.

### 14.17 Completion checklist

- [x] Redis pool reused (not duplicated), config-driven timeouts/retry/health-check, graceful shutdown
- [x] `CacheKeyBuilder` — centralized, collision-safe, predictable, with SCAN patterns
- [x] `CacheSerializer` — JSON not pickle, versioned envelope, unknown version = miss
- [x] `CacheService` — get/set/delete/exists/expire/increment/get_or_set/invalidate, every failure logged + degraded
- [x] `CachePolicy` — per-entity TTLs, all config-driven
- [x] `CacheInvalidator` — operation-named fan-out, delete-never-update
- [x] Cache-aside with single-flight stampede protection and bounded follower wait
- [x] `DistributedLockService` — bounded acquire, ownership validation, strict release, contention vs infrastructure separation
- [x] `RateLimiter` — atomic Lua token bucket, per-category budgets, fail-open/closed configurable
- [x] Real rate limiting replacing the Phase 4 no-op, wired to auth / metadata / folders / upload initiate+complete / search as dependencies
- [x] Service integration: user, folder metadata/children/breadcrumbs, file metadata, versions, search
- [x] Authorization preserved and re-derived on every cached read, analyzed per entity type
- [x] All settings in `Settings` + `.env.example` + `k8s/05-configmap.yaml`
- [x] Structured, metrics-ready logs for every cache/lock/rate-limit event; identities hashed
- [x] 101 new tests, 246/246 passing, `FakeRedisClient` extended with failure injection + controllable clock
- [x] Benchmark script + runbook with **no fabricated numbers**
- [x] `docs/PHASE_7_REDIS_DESIGN.md`, this section, `CONTEXT.md`
- [ ] Post-commit invalidation, descendant-breadcrumb fan-out, negative caching, probabilistic early expiration, cache warming, metrics backend — known gaps, deferred

## 15. Event-Driven Architecture: Pub/Sub + Transactional Outbox *(Phase 8)*

Through Phase 7, every consequence of an upload happened *inside* the
upload request. Phase 8 moves the non-critical consequences out: the API
records that something happened and returns; separate worker processes
decide what to do about it.

The full engineering rationale — sequence diagrams, the dual-write hazard
analysis, ack-timing, the DLQ runbook, ordering and versioning, an
eleven-scenario failure catalogue — lives in
**[`docs/event-driven-architecture.md`](docs/event-driven-architecture.md)**.
This section is the walkthrough.

### 15.1 The problem this phase exists to solve

Thumbnailing an image takes seconds. Sending a notification depends on a
third party. Neither is something the user's upload should wait for, and
neither is something an upload should *fail* for. But the naive fix —
publishing a message at the end of the request handler — introduces a
worse bug than the one it solves:

```python
# The dual write. Do not do this.
await session.commit()          # (1) Postgres says the file exists
await publisher.publish(event)  # (2) ...and then the process dies
```

There is no transaction spanning Postgres and Pub/Sub. Crash between (1)
and (2) and the file exists forever with no thumbnail and no
notification, and **nothing anywhere knows**. Swap the order and you get
the mirror-image bug: a `thumbnail.requested` for a file that was never
committed, which every consumer will fail on permanently.

The transactional outbox removes the second write entirely. The event is
inserted as a **row in the same database transaction as the business
data**. One commit, one atomic outcome. A separate process reads those
rows and publishes them.

> The outbox does not make delivery reliable. It makes the *decision to
> deliver* atomic with the business fact. Delivery is then a separate,
> retryable problem — which is a problem with a known solution, unlike
> "we lost the fact that this happened."

### 15.2 Architecture

```
   HTTP request
        │
        ▼
  ┌───────────────────────────────────────────────────┐
  │  FastAPI (Phases 1-7, unchanged)                  │
  │                                                   │
  │   ┌────────────── ONE TRANSACTION ─────────────┐  │
  │   │  INSERT file_metadata  ...                 │  │
  │   │  INSERT file_versions  ...                 │  │
  │   │  INSERT outbox_events (status=PENDING) ◄───┼──┼── the only new write
  │   └──────────────────┬─────────────────────────┘  │
  │                   COMMIT                          │
  └───────────────────────┼───────────────────────────┘
                          ▼
              ┌───────────────────────┐
              │  PostgreSQL           │  outbox_events
              │  (authoritative)      │  processed_events
              └───────────┬───────────┘  notifications
                          │ poll (FOR UPDATE SKIP LOCKED)
                          ▼
              ┌───────────────────────┐
              │ outbox-publisher      │  publish → mark PUBLISHED → commit
              └───────────┬───────────┘  (per row, never per batch)
                          ▼
   ┌──────────────────────────────────────────────────────┐
   │                  Google Cloud Pub/Sub                 │
   │   file-events        upload-events    notification-   │
   │                                        events         │
   └────┬──────────────────────┬──────────────────┬────────┘
        │                      │                  │
        ▼                      ▼                  ▼
  ┌───────────────┐   ┌─────────────────┐  ┌──────────────────┐
  │ file-worker   │   │ thumbnail-worker│  │ notification-    │
  │ verify bytes  │   │ Pillow decode   │  │ worker           │
  │ fan out ──────┼──►│ write thumbnail │  │ render + persist │
  │               │   │ update metadata │  │ (stub delivery)  │
  └───────┬───────┘   └─────────────────┘  └──────────────────┘
          └──── publishes thumbnail.requested / notification.requested
               DIRECTLY (worker-to-worker, no outbox — see §15.6)
```

Every worker is the **same container image** as the API, started with a
different `python -m app.workers.<name>` command. One build, one
dependency set, one copy of the SQLAlchemy models that read the same
tables.

### 15.3 The event flow, end to end

1. `POST /files/upload` writes `FileMetadata`, `FileVersion` and an
   `OutboxEvent(file.uploaded, PENDING)` — one transaction, one commit.
2. `outbox-publisher` polls, publishes the row to `file-events`, marks it
   `PUBLISHED`, commits. Per row, not per batch.
3. `file-processing-worker` consumes it, does one cheap GCS metadata HEAD
   to confirm the bytes really landed, cross-checks size/content-type
   against what the event claimed, and publishes `thumbnail.requested`
   (images only) plus `notification.requested`.
4. `thumbnail-worker` downloads, decodes, resizes, writes
   `thumbnails/{file_id}.png`, then sets
   `FileMetadata.thumbnail_object_name`. In that order — see §15.9.
5. `notification-worker` renders a subject/body and writes a
   `Notification` row.

Each consumer records a `ProcessedEvent(event_id, consumer_name)` in the
same transaction as its work. That row is what makes a redelivery a
no-op.

`tests/test_events_integration.py` drives exactly this chain, stage by
stage, through the real components.

### 15.4 Event catalog

| Event | Producer | Topic | Emitted from |
|---|---|---|---|
| `file.uploaded` | api | file-events | `FileUploadService.upload_file` |
| `file.completed` | api | file-events | `ChunkedUploadService._finalize` |
| `upload.completed` | api | upload-events | `ChunkedUploadService._finalize` |
| `file.version.created` | api | file-events | `FileUploadService.replace_file`, `MetadataService.update_metadata` |
| `file.deleted` | api | file-events | `MetadataService.delete_file` |
| `file.restored` | api | file-events | `MetadataService.restore_file` |
| `file.moved` | api | file-events | `MetadataService.move_file` |
| `file.renamed` | api | file-events | `MetadataService.rename_file` |
| `folder.created` | api | file-events | `FolderService.create_folder` |
| `folder.deleted` | api | file-events | `FolderService.delete_folder` |
| `thumbnail.requested` | file-worker | file-events | fan-out, direct publish |
| `notification.requested` | file-worker | notification-events | fan-out, direct publish |

Names are `{aggregate}.{past-tense-verb}`: events describe what **has
already happened**, never a command. The two `*.requested` values are the
deliberate exception — they are worker-to-worker work requests, and the
name makes that visible in a log line without knowing the publisher.

**Three topics, not one firehose and not twelve.** One topic would force
every worker to receive and discard everything, coupling a slow thumbnail
consumer to notification latency. Twelve would make adding an event type
a Terraform change for no isolation benefit. Three is the number of
genuinely distinct fan-out boundaries today — and `notification-events`
is separate specifically because it is an **egress** boundary: a wedged
email provider must never apply backpressure to file processing.

The envelope (`app/events/envelope.py`) carries `event_id`,
`event_type`, `event_version`, `occurred_at`, `producer`,
`correlation_id`, `causation_id`, `tenant_id` (reserved, always null),
`user_id`, and a free-form `payload`. Everything a consumer *framework*
needs is a typed envelope field; everything a specific *handler* needs is
in the payload. That split is what lets `BaseWorker` parse, deduplicate,
log and route a message whose semantics it knows nothing about.

`correlation_id` and `causation_id` are different things and both matter:
correlation ties the whole tree back to one client operation (read from
the same `structlog.contextvars` `RequestContextMiddleware` already
binds, so a worker's logs join the user's original HTTP request with no
manual threading); causation is one **edge** in that tree — the event
that directly caused this one.

### 15.5 Why an outbox, and what it does *not* buy

`OutboxRepository.add_event` is deliberately boring: `session.add()` +
`flush()`, exactly like every other repository here. No commit, no
transaction of its own. That is the whole trick — the repository is built
from the request-scoped session, and the only `session.commit()` in the
entire application is in `app/database/session.py::get_db`. Phase 8 added
**no transaction-management code at all**; it exploits the Unit of Work
that was already there.

Alternatives considered and rejected:

- **Publish inside the request after commit** — the dual write above.
- **Publish before commit** — publishes events for transactions that then
  roll back. Strictly worse: now consumers act on facts that never
  happened.
- **Postgres logical decoding / Debezium CDC** — genuinely the more
  scalable answer at high volume, and genuinely more operational surface
  (a connector to run, schema-evolution coupling, a replication slot that
  can silently fill your disk if a consumer stalls). For one service at
  this volume, a polled table is the right size of solution.

What the outbox does **not** buy: exactly-once delivery. It cannot. If
the publisher dies after Pub/Sub accepts a message but before
`mark_published` commits, the row stays `PENDING` and is republished next
poll. That is deliberate — the alternative (mark published *before*
publishing) trades a harmless duplicate for a silently lost event. The
whole system is at-least-once, and idempotent consumers are the
counterpart that makes it correct.

### 15.6 Why the fan-out bypasses the outbox

`thumbnail.requested` and `notification.requested` are published
**directly** by the file worker, not through an outbox row. This looks
inconsistent until you ask what the outbox is for: making a Postgres
write atomic with a publish. At that point in the chain there is **no
competing Postgres write** — the file worker validates and forwards, it
mutates no business data. With nothing to be atomic with, an outbox row
would add a table write, a poll interval of latency and a second
process's involvement, and buy nothing.

The failure mode is bounded and already handled: a failed fan-out publish
NACKs the whole message, Pub/Sub redelivers, and the fan-out re-runs.

### 15.7 Idempotency: the part that is easy to get subtly wrong

Pub/Sub is at-least-once. Duplicates are not an edge case, they are the
contract. Three mechanisms, in order of how load-bearing they are:

1. **`ProcessedEvent`'s `UniqueConstraint(event_id, consumer_name)` is
   the real guarantee.** The `has_processed` pre-check is an
   *optimization only*. Two replicas can both pass the pre-check
   concurrently; only one can win the insert. The loser catches
   `IntegrityError`, logs a duplicate, and **still ACKs** — a NACK there
   would request work that is definitionally already done.
2. **Keyed per `(event_id, consumer_name)`, not per `event_id`.** Three
   consumers legitimately process the same event. A ledger keyed on
   `event_id` alone would let whichever worker arrived first block the
   other two.
3. **Derived event IDs are deterministic** — UUIDv5 over
   `(parent_event_id, child_event_type)`. This is the subtle one. A
   `uuid4()` there would give every retry of the fan-out a fresh
   identity, so downstream deduplication would never fire and every
   redelivery would regenerate the thumbnail forever. Nothing downstream
   would notice; it would just cost money. There is a test whose only job
   is to catch that regression.

Work and ledger row commit **together**, in one transaction owned by
`BaseWorker._handle` — the same "one commit at the boundary" discipline
`get_db` enforces for the API. A consumer that committed its own work
separately could crash between the two and re-notify a user.

### 15.8 Retries, ack timing, and the dead-letter queue

`BaseWorker` makes the ack/nack decision in exactly one place. If each
worker made it for itself they would drift, and an inconsistent ack
policy is how events get silently dropped.

| Outcome | `ProcessedEvent` | Settle |
|---|---|---|
| pre-check hit (already processed) | (exists) | ACK |
| `process()` returns | SUCCEEDED | ACK |
| `NonRetryableEventError` | FAILED | ACK |
| envelope fails to parse | none | ACK |
| anything else | none | NACK |
| lost idempotency race | (winner's) | ACK |

**ACK after processing, never before.** Acking on receipt is the classic
way to lose work: the message is gone from Pub/Sub the instant the
process dies. Acking after means a crash mid-message costs a redelivery,
which idempotency makes free.

**The retryable/non-retryable split is where most of the judgment is.**
Infrastructure failures (GCS timeout, DB unreachable, Pub/Sub publish
failure) are retryable — NACK, and Pub/Sub redelivers with backoff.
Content and contract failures are permanent: a malformed envelope, a
payload missing its own required fields, an unsupported content type, a
`FileMetadata` row that no longer exists. Redelivering those produces the
same failure forever. Getting this backwards in *either* direction is
expensive: classifying a transient error as permanent drops real work;
classifying a permanent error as transient burns delivery attempts and
eventually fills a DLQ with messages no human can fix.

**Non-retryable errors are ACKed and recorded, never dead-lettered.** The
DLQ is for *retry-exhausted* messages — the ones where a human might fix
something and replay. A permanently unsupported file type will never
succeed no matter how many times it is redelivered; putting it in the DLQ
turns the DLQ into noise, and the noise is what makes teams stop reading
it. The durable record is a queryable `ProcessedEvent(status=FAILED,
error=...)` row instead. See the DLQ runbook in
`docs/event-driven-architecture.md`.

### 15.9 Ordering that matters, and ordering that does not

**No Pub/Sub ordering keys this phase.** The event catalog was audited
rather than assumed: every consumer shipped here is an idempotent
projection with no cross-event sequencing need. A thumbnail is generated
from the object's *current* bytes; a notification row is append-only;
file validation reads current state. Ordering keys serialize delivery per
key and cap throughput at one in-flight message per aggregate — a real
cost for a guarantee nothing needs yet. `aggregate_id` is captured on
every outbox row anyway, so enabling ordering later is a publisher-side
change with no migration.

Ordering *within* a worker is a different question, and it does have a
right answer. The thumbnail worker generates and uploads the thumbnail
**first**, then points the metadata row at it. Reversed, a crash between
the two would leave `thumbnail_object_name` referencing an object that
does not exist — a dangling pointer served to users. In this order the
worst outcome is an orphaned object nothing references, costing a few
kilobytes, which the next redelivery overwrites in place because the
thumbnail object name is deterministic.

### 15.10 Thumbnails, and why the allow-list comes first

`ThumbnailService` supports exactly four raster types: `image/jpeg`,
`image/png`, `image/webp`, `image/gif`. Anything else raises
`NonRetryableEventError` **before any download and certainly before any
decode**. That ordering is a security property, not an optimization —
Pillow must never be handed bytes of an unknown format on the strength of
a client-declared MIME type. `Image.open(formats=...)` then pins the
decoder to the declared type, so a PNG relabelled as a JPEG is rejected
rather than sniffed and decoded anyway.

The thumbnail worker is a separate Deployment with a 1Gi memory limit
(≈4x the others) for the same reason: a 50-megapixel image expands to
hundreds of megabytes decoded, and co-locating that with file validation
would force the limit onto every fan-out pod and let one bad image
OOM-kill it.

### 15.11 Notifications: a stub with a real seam

`NotificationSender` is a one-method abstraction; `LoggingNotificationSender`
is the only implementation this phase ships. It writes a `Notification`
row and logs `would send email (stub)`. There is **no SMTP, no
SendGrid/SES/FCM, no template engine, no delivery retry** — deliberately.
Secrets management, bounce and complaint handling, unsubscribe compliance
and per-provider retry semantics are an entire phase's worth of concerns,
and none of them are about event plumbing.

The seam exists anyway because it costs one class, and because the row is
genuinely useful rather than a placeholder that does nothing: it makes
the whole chain assertable in a test and queryable by an operator ("did
the notification path actually run for this upload?").

### 15.12 Configuration and the kill switch

`PUBSUB_ENABLED` defaults to **false**. With it off, `EventPublisher` is
a logged no-op and the API behaves exactly as it did in Phases 1-7 —
outbox rows are still written transactionally, they simply never leave
Postgres. This is why Phase 8 can land dark: flipping a config value, not
deploying new code, turns the system on, and flipping it back stops all
event traffic while events accumulate durably and drain when it returns.
Same operational pattern as Phase 7's `CACHE_ENABLED`.

Full variable list: §18. All of it is mirrored into
`k8s/05-configmap.yaml`.

### 15.13 Running it locally

`docker-compose.yml` now ships a `pubsub-emulator` service plus the four
workers:

```bash
# .env — the emulator needs no credentials, which is the point
PUBSUB_ENABLED=true
PUBSUB_EMULATOR_HOST=pubsub-emulator:8085
GCP_PROJECT_ID=nimbusfs-dev

docker compose up -d postgres redis pubsub-emulator
# create topics + subscriptions once (the emulator starts empty):
docker compose exec pubsub-emulator bash -c '
  export PUBSUB_EMULATOR_HOST=localhost:8085
  python -m pip install --quiet google-cloud-pubsub
  python - <<PY
from google.cloud import pubsub_v1
p, s = pubsub_v1.PublisherClient(), pubsub_v1.SubscriberClient()
project = "nimbusfs-dev"
topics = ["nimbusfs-file-events", "nimbusfs-upload-events", "nimbusfs-notification-events"]
subs = [("nimbusfs-file-events", "nimbusfs-file-events-file-worker-sub"),
        ("nimbusfs-file-events", "nimbusfs-file-events-thumbnail-worker-sub"),
        ("nimbusfs-notification-events", "nimbusfs-notification-events-notification-worker-sub")]
for t in topics:
    p.create_topic(name=p.topic_path(project, t))
for t, sb in subs:
    s.create_subscription(name=s.subscription_path(project, sb), topic=p.topic_path(project, t))
PY'

docker compose up -d app worker-outbox-publisher worker-file-processing \
                     worker-thumbnail worker-notification
docker compose logs -f worker-thumbnail
```

`PUBSUB_EMULATOR_HOST` is read by the **client library**, not by NimbusFS
code — `EventPublisher.build_publisher_client` exports it into the
process environment because that is the only channel google's client
reads it from. Nothing in the application knows it is not talking to
Google.

The emulator is in-memory: topics, subscriptions and unacked messages are
lost on restart. That is fine, and it is exactly why the outbox lives in
Postgres — a wiped emulator loses *messages*, never *events*.

**This was written but never run.** No Pub/Sub emulator, no Postgres, no
Docker was started in any Phase 8 session. See §15.16.

### 15.14 On GKE

`k8s/16-worker-serviceaccounts.yaml` through
`k8s/21-deployment-notification-worker.yaml`. Four Deployments, four
ServiceAccounts, four RoleBindings; no Service, no Ingress, no readiness
probe for any of them — workers pull from Pub/Sub and nothing connects to
them.

IAM is where the real least-privilege story is, and it is per-worker on
purpose:

| Worker | Pub/Sub | GCS |
|---|---|---|
| outbox-publisher | publisher on all 3 topics | none |
| file-worker | subscriber (file-worker-sub) + publisher (file, notification) | objectViewer |
| thumbnail-worker | subscriber (thumbnail-sub) | objectViewer + objectCreator on `thumbnails/` only |
| notification-worker | subscriber (notification-sub) | none |

Read that as a blast-radius statement. A compromised notification worker
cannot read one user's file, because it has no GCS role at all — and it
is the component that will one day talk to a third party, so it is the
most exposed. One shared worker service account would hand every worker
the union of all four.

Other deliberate differences from the API Deployment:

- **Default RollingUpdate**, not the API's `maxUnavailable:0/maxSurge:1`.
  That tuning keeps HTTP traffic flowing through a deploy; there is no
  traffic here. A worker killed mid-message does not ack, Pub/Sub
  redelivers, and the ledger makes the reprocessing a no-op.
- **Liveness only, as an exec probe on a heartbeat file** touched on a
  timer independent of message arrival. An idle worker on an empty
  subscription is healthy; a probe keyed on "processed a message
  recently" would restart every worker on a quiet night. The probe checks
  the file's *mtime*, not merely its existence, so a wedged event loop is
  caught instead of papered over.
- **`command:` in exec form**, so PID 1 is Python itself — a shell
  wrapper would swallow SIGTERM and the graceful drain would never run.
- **One shared ConfigMap**, extended, not a second worker ConfigMap.
  Topic names are shared vocabulary, and a producer and consumer
  disagreeing about a topic name is a silent total delivery failure — two
  ConfigMaps is exactly how that disagreement happens. Genuinely
  per-worker values (the thumbnail worker's `WORKER_CONCURRENCY=3`,
  reduced from 10 because ten concurrent decodes against a 1Gi limit is
  an OOMKill waiting for a busy hour) are a small per-Deployment `env:`
  block, which takes precedence over `envFrom`.

### 15.15 Failure scenarios

Full catalogue in `docs/event-driven-architecture.md`; the headline cases:

| Failure | What happens |
|---|---|
| Pub/Sub unavailable | Uploads keep working. Outbox rows accumulate `FAILED` with exponential backoff into `next_attempt_at` and drain when it returns. Nothing is lost. |
| Publisher crashes after publish, before commit | Row stays `PENDING`, republished next poll. Consumers absorb the duplicate on `event_id`. |
| Worker crashes mid-message | No ack, redelivery, reprocessed. `process()` is idempotent by contract. |
| Worker crashes after work, before ack | Same — but the work *and* its ledger row committed together, so the redelivery hits the pre-check and is skipped. |
| Postgres unavailable | The API is down anyway (it is authoritative). Workers NACK everything and the subscription backlog grows; nothing is lost. |
| GCS unavailable | Thumbnail worker NACKs (retryable). A *missing object* is different — that is permanent, ACK + `FAILED` row. |
| Duplicate event | Absorbed by `ProcessedEvent`. Losing a race on the unique constraint still ACKs. |
| Poison message | Unparseable bytes: logged at ERROR and ACKed. Redelivering the same bytes produces the same bytes. |
| Retry exhausted | Pub/Sub routes to the DLQ after `MAX_DELIVERY_ATTEMPTS`. Human triage, then replay. |

### 15.16 What is NOT built, and what was never verified

Stated plainly, because a claim of completeness that quietly excludes
this list is worse than the gaps themselves:

- **No live infrastructure was ever run.** Across both Phase 8 sessions,
  no Pub/Sub emulator, no real Pub/Sub, no Postgres, no Docker, no GKE
  cluster. Everything is verified against in-memory SQLite and
  hand-written fakes. The `docker-compose.yml` additions and the six k8s
  manifests are written and internally consistent; neither has been
  started or applied. Migration `0005` has never been applied to a real
  database.
- **No autoscaling on backlog.** No HPA for any worker, and no
  `num_undelivered_messages` custom metric — the natural scaling signal
  for a consumer, and the natural next step.
- **No real notification provider.** Stub sender only (§15.11).
- **No DLQ replay tooling.** The runbook documents the `gcloud` steps;
  there is no script, and nothing cross-region.
- **Thumbnails cover four raster MIME types.** No PDF, SVG, video
  keyframe, HEIC or TIFF.
- **No reconciliation job.** Phase 6's known gap — an upload session
  stuck mid-`COMPLETING` after a process crash — is now *possible* to fix
  with workers in place, but was not fixed here.
- **No metrics backend.** Every event is structured-logged with
  metrics-ready fields; nothing scrapes them.

### 15.17 Testing

`tests/test_events_envelope.py`, `test_event_publisher.py`,
`test_outbox_repository.py`, `test_processed_event_repository.py`,
`test_event_emission.py`, `test_outbox_publisher_worker.py`,
`test_base_worker.py`, `test_file_processing_worker.py`,
`test_thumbnail_worker.py`, `test_notification_worker.py`, and
`test_events_integration.py` — **410/410 passing, zero regressions**
against the pre-existing 246.

`tests/fakes/fake_pubsub.py` follows the same philosophy as the GCS and
Redis fakes: it really *stores* messages per topic, so a test asserts
"this exact envelope landed on this exact topic," not "publish was called
once." Its `publish()` returns a `concurrent.futures.Future` — not a
coroutine — precisely because that is what the real client returns and
what `EventPublisher` must bridge with `asyncio.wrap_future`; faking it
as awaitable would let a genuine event-loop-blocking bug pass.

It deliberately does **not** simulate Pub/Sub's server behavior — no ack
deadlines, no automatic redelivery, no DLQ routing. Those are Google's
semantics, and faking them would be asserting our guesses about them.
Where redelivery matters, a test hands the same message to the worker
twice explicitly: a guaranteed duplicate is a stronger test than a
probabilistic one.

The thumbnail tests use **real Pillow on real bytes** — genuine JPEG/PNG/
WebP/GIF generated in memory, thumbnails decoded back and measured, plus
truncated files, empty objects and a PNG mislabelled as a JPEG. Mocking
Pillow would test nothing; the entire risk in that component is what a
decoder does with real and deliberately malformed input.

`test_events_integration.py` is the one that proves the phase works as a
*system*: it drives an image upload through the real FastAPI client, then
each stage of the chain in turn against the shared fakes, asserting the
outbox row, the published message, the fan-out, the rendered thumbnail
and the notification row. It also runs the whole chain twice to prove
redelivery changes nothing, and cuts Pub/Sub off mid-flight to prove the
event stays durable and replayable.

### 15.18 Completion checklist

- [x] `EventEnvelope` + 12-value `EventType` catalog + topic routing table
- [x] `OutboxEvent`/`ProcessedEvent`/`Notification` models + migration `0005` (never run against real Postgres)
- [x] `OutboxRepository` (`FOR UPDATE SKIP LOCKED`, backoff) + `ProcessedEventRepository` (SAVEPOINT-guarded record)
- [x] `EventPublisher` — executor-wrapped sync client, `PUBSUB_ENABLED` kill switch, one normalized exception type
- [x] Emission wired into all 9 hook points via optional `outbox=` kwarg — zero pre-existing call sites or tests changed
- [x] `outbox-publisher` worker: per-row commit, exponential backoff, survives Postgres being down
- [x] `BaseWorker`: streaming pull, thread→asyncio bridge, per-message log context, one ack policy
- [x] File processing, thumbnail (Pillow, 4 types, allow-list before decode) and notification (stub sender) workers
- [x] Idempotency: unique constraint as the guarantee, pre-check as optimization, deterministic derived event IDs
- [x] Graceful shutdown + heartbeat-file liveness, shared by all four workers
- [x] Docker Compose: emulator + 4 worker services — **written, not run**
- [x] k8s 16–21: per-worker KSA/GSA scoping, RBAC, 4 Deployments, ConfigMap + README extended — **written, not applied**
- [x] 164 Phase 8 tests, 410/410 passing, zero regressions
- [x] `docs/event-driven-architecture.md`, this section, `CONTEXT.md`
- [ ] Backlog-based autoscaling, real notification provider, DLQ replay tooling, live-infrastructure verification — known gaps, deferred

## 16. High Availability & Disaster Recovery *(Phase 9)*

Phase 9 does not add a new feature to the product — it hardens
everything Phases 1–8 already built against infrastructure failure, and
designs (without yet running) the procedure for recovering from failure
too large for that hardening to absorb. Full depth lives in four
standalone documents this section summarizes and links to:
[`docs/high-availability.md`](docs/high-availability.md),
[`docs/disaster-recovery.md`](docs/disaster-recovery.md),
[`docs/failure-testing.md`](docs/failure-testing.md), and
[`docs/backup-restore.md`](docs/backup-restore.md).

**Read every claim below through one lens**: DESIGNED (a document/config
says so), IMPLEMENTED (code/manifests enforce it), TESTED (a test in
this repo's suite exercises it, against fakes), or MEASURED (a real
number from a real failure/restore against real infrastructure). As of
this phase, **nothing is MEASURED** — no real GKE cluster, Cloud SQL
instance, Memorystore instance, or Pub/Sub was available in this
session, the same constraint every prior phase has stated for its own
infrastructure claims. See `docs/high-availability.md` §16 and
`docs/disaster-recovery.md` §14 for exactly what a future session with
real GCP access should run to convert targets into measurements.

### 16.1 HA vs. DR — the distinction this phase insists on

**High Availability**: does the system keep serving traffic through a
normal, expected infrastructure failure (a Pod crash, a node crash, one
zone going down, a Redis/Cloud SQL blip)? **Disaster Recovery**: can the
system be brought back after a failure too large for HA to absorb (an
entire region, a corrupted database, a mass-deletion incident)? These
are evaluated independently throughout — a system can have excellent HA
and no DR story (three zones, zero backups) or the reverse, and
conflating them is how one gets mistaken for the other. See
`docs/high-availability.md` §1 for the full table.

### 16.2 Availability target

**99.9% monthly** for the API tier (≈8h 46m/year downtime budget), not
99.95%/99.99% — the ceiling is Cloud SQL regional HA's recurring
failover time (§16.4), and claiming a tighter number on top of a
single-writer regional Postgres without read/write separation (still
designed-not-wired since Phase 4, §11) would be a number the
architecture cannot back. Full derivation: `docs/high-availability.md` §2.

### 16.3 RTO / RPO

**RTO < 4 hours, RPO < 1 hour**, for a full regional failover — the
worst case `docs/disaster-recovery.md` covers. RPO is bounded by Cloud
SQL PITR (continuous transaction log, not just daily backups); RTO
reflects a genuinely **manual** warm-standby failover (a human executing
a documented runbook), not an automated one. Full derivation:
`docs/disaster-recovery.md` §1.

### 16.4 Multi-zone GKE

Phase 5 already assumed a regional (multi-zone) GKE cluster with soft
`podAntiAffinity`; Phase 9 adds `topologySpreadConstraints`
(`maxSkew: 1` on `topology.kubernetes.io/zone`) to the API Deployment and
all four Phase 8 worker Deployments (`k8s/07`, `k8s/18-21`) — anti-
affinity alone expresses only a relative preference and can clump once a
zone looks "different enough"; skew constraints give an absolute bound
that holds at any HPA-scaled replica count. Both are soft
(`ScheduleAnyway`/`preferred...`), never hard, for the same reason Phase
5 chose soft: a temporarily degraded zone must never leave a Pod
permanently `Pending`.

Two workers moved from 1 replica to 2 for the same zone-redundancy
reason (`outbox-publisher`, `notification-worker` — see each
Deployment's own Phase 9 header comment for the specific trade-off);
`file-worker`/`thumbnail-worker` were already at 2 since Phase 8 and only
gained the topology spread addition. `k8s/23-pdb-workers.yaml` (new) adds
a `minAvailable: 1` PodDisruptionBudget per worker now that each runs
>=2 replicas, mirroring the API's pre-existing `10-pdb.yaml`.

### 16.5 Cloud SQL, Memorystore HA, and in-app failure handling

Cloud SQL **Regional (HA)** configuration (primary + standby, automatic
promotion typically under a minute) and Memorystore **Standard tier**
(replica + automatic failover, same order of magnitude) are the intended
managed-service configuration — both are `gcloud`-level configuration
this session had no real instance to apply, documented in full in
`docs/high-availability.md` §5/§7 including the honest caveat that
"automatic failover" does not mean "zero application-visible impact":
existing connections error, and the app's pre-existing retry/backoff
(`retry_async`, Phase 4) and connection pool absorb the gap over tens of
seconds, not instantly.

**What is genuinely IMPLEMENTED and TESTED already** (Phase 7 work,
Phase 9 confirms and cross-references it rather than rebuilding it):
cache reads fall through to Postgres on Redis failure
(`CacheService`), distributed locks fail SAFE — `ServiceUnavailableException`
(503), never "proceed as if held" — on Redis failure at acquisition
(`DistributedLockService`), and rate limiting degrades per the
configurable `RATE_LIMIT_FAIL_OPEN` (default: fail-open, an explicit,
documented security trade-off — see `docs/high-availability.md` §8 for
why failing closed would be worse here).

### 16.6 Pub/Sub and worker resilience

Unchanged in mechanism from Phase 8 — the transactional outbox already
means a Pub/Sub or worker outage delays events, never loses them
(README §15). Phase 9's only change here is the replica-count bump in
§16.4; the idempotent-consumer guarantee (`ProcessedEvent`'s unique
constraint) that makes worker-crash-mid-message safe was already
IMPLEMENTED + TESTED in Phase 8 (`tests/test_base_worker.py`).

### 16.7 Reconciliation

**New this phase**: `app/services/reconciliation_service.py` +
`app/workers/reconciliation_job.py`, run every 6 hours as
`k8s/22-cronjob-reconciliation.yaml`. A read-only, keyset-paginated
(never `OFFSET`) walk of every non-deleted, `upload_status=COMPLETED`
`FileMetadata` row, confirming each one's GCS object still exists.
Detects only the dangerous drift direction — a metadata row pointing at
a missing object, which 404s a user mid-download — not the inverse
(orphaned GCS objects with no owning row, which costs storage money, not
correctness, and requires a full bucket listing this codebase has never
needed; explicitly left for a future phase, see
`docs/disaster-recovery.md` §5.2).

**The service has no delete/update code path anywhere in its call
graph** — not "delete gated behind a flag," no delete statement exists
at all. `tests/test_reconciliation.py` (6 tests) proves both the
detection logic and, explicitly, that a flagged row is byte-for-byte
unchanged after a run (`test_never_mutates_or_deletes_anything`). The
job's exit code (0 clean / 1 issues found / 2 scan incomplete) is the
machine-readable hook for a future alerting pipeline. Its own KSA/GSA
(`nimbusfs-reconciliation-ksa`, `k8s/16`/`k8s/17`) is read-only on both
systems it inspects — `roles/storage.objectViewer` and
`roles/cloudsql.client`, nothing more — so a future phase that wants to
*act* on a finding (quarantine, re-upload, delete) has to widen that
grant deliberately, in lockstep with the code gaining the ability to act.

### 16.8 Multi-region DR: active-passive, warm standby

Selected over cold standby (RTO too slow once you count provisioning
from scratch) and over active-active (rejected — no requirement here
justifies solving multi-writer Postgres consistency, and doing so
"because it sounds enterprise-grade" is exactly what this phase's own
brief warns against). A warm-standby region keeps a minimal-replica-count
GKE deployment running, restores Cloud SQL from a cross-region-replicated
backup on demand (not a continuously-replicated standby — that would
blur into active-active's cost without the RTO win), and reuses the
existing Global external Application Load Balancer (already Phase 5
infrastructure) to add the DR region's backend once promoted, rather
than pre-provisioning an idle second region's frontend. Full design,
the failover runbook, DNS-failover caveats, and secrets/IAM-for-DR
guidance (Secret Manager + Workload Identity, never a copied service-
account key between regions): `docs/disaster-recovery.md` §6–§10.

### 16.9 GCS durability & protection

GCS's own object durability is already extremely high regardless of
bucket location type — the real decision is availability during a
*regional* outage. **Recommendation: keep the existing regional bucket,
add a scheduled regional-to-regional object-replication job to the DR
region** (not a dual-region or multi-region bucket, which is either
higher-latency for every write or optimized for a global-read-locality
problem NimbusFS doesn't have) — designed and documented, not
implemented this phase. Recommended bucket-level protections: GCS
Object Versioning + a lifecycle rule aging out noncurrent versions, and
GCS's own bucket-level soft-delete — both are a safety net against
mutation/deletion *outside* the application (a `gsutil rm`, a
compromised credential) that the application's own dedup/rollback
guardrails (Phase 3) cannot see, since those bypass the application
entirely. Full reasoning and cost trade-off: `docs/disaster-recovery.md`
§2–§3.

### 16.10 Failure matrix, monitoring, alerting, security

A complete failure matrix (Pod/Node/Zone/Cloud SQL/Memorystore/Pub-Sub/
GCS/Worker/Region — impact, detection, recovery) is in
`docs/high-availability.md` §10. A metric inventory (not a running
dashboard — no observability stack ships this phase, same as Phases 5/7/
8) and a severity-tiered (P0–P3) alert list are in §11–§12 there. A
short security review confirms no HA/DR mechanism in this phase weakens
IAM, Workload Identity, TLS, private networking, or authentication —
every one of those properties came from decisions already made in
Phases 1–8 and simply carries over unchanged (§13 there).

### 16.11 Cost analysis

Architecture-level, no fabricated GCP pricing: multi-zone (this phase)
is roughly compute-cost-neutral (same total replica count, just spread)
with a real ~2x-and-up database/Redis cost premium for Regional HA/
Standard tier; multi-region DR adds cross-region storage/egress as its
largest new line item. Full comparison table and the recommendation
(multi-zone as the default, multi-region only if the business accepts
the added operational complexity for an RTO/RPO a single region can't
meet): `docs/high-availability.md` §14.

### 16.12 Testing

Chaos-testing procedures for all 13 scenarios from the brief (delete a
Pod, kill/drain a node, simulate a zone/Redis/Cloud-SQL/GCS/Pub-Sub
failure, kill a worker mid-message, restore from backup, restore in a
secondary region), each labeled LOCAL/TEST, STAGING, or PRODUCTION with
commands and pass criteria, plus an RTO/RPO measurement template and an
executable, STAGING-only backup/restore drill (7 steps, real `gcloud`/
`curl` commands) live in `docs/failure-testing.md` and
`docs/backup-restore.md`. **None have been executed this session** — see
each document's own completion checklist for exactly what remains
DESIGNED rather than MEASURED, and §16.14 below for the code-level tests
that do run today.

### 16.13 Config (`.env.example` / `k8s/05-configmap.yaml`)

Four new settings, all additive, all defaulted so no existing deployment
breaks: `RECONCILIATION_ENABLED` (default `true`), `RECONCILIATION_DRY_RUN`
(default `true` — a documented seam for a future apply-mode; there is no
code path that deletes/mutates regardless of this flag's value today),
`RECONCILIATION_BATCH_SIZE` (default `500`), `RECONCILIATION_MAX_ISSUES`
(default `5000`, bounds worst-case GCS API calls on a badly-corrupted
dataset). Mirrored into `k8s/05-configmap.yaml` exactly like every prior
phase's new settings.

### 16.14 Tests

6 new tests, `tests/test_reconciliation.py`: clean state reports no
issues, a missing object is flagged, soft-deleted and still-pending rows
are correctly skipped (not false positives), keyset pagination correctly
walks multiple batches, and — the one that matters most — the service
never mutates or deletes the row it flagged. **416/416 passing, zero
regressions** against all 410 pre-Phase-9 tests.

### 16.15 What Phase 9 does NOT include

No CI/CD pipeline, no full observability/metrics backend, no AI
features, no multi-cloud, no active-active multi-region, no service
mesh, no Terraform (all explicitly out of scope per this phase's own
brief). No orphaned-GCS-object detection (the reconciliation direction
deliberately left for later, §16.7). No automatic remediation of a
reconciliation finding — a human decides, every time, this phase. No
actual database failover, Redis failover, or regional failover was
triggered against real infrastructure — every HA/DR claim in this
section is DESIGNED and, where noted, IMPLEMENTED/TESTED against fakes,
never MEASURED.

### 16.16 Design decisions

Soft scheduling constraints throughout, never hard (§16.4's reasoning
recurs from Phase 5). Two workers bumped 1→2 replicas with each
Deployment's own explained trade-off, not a blanket "everything gets N
replicas" policy. Reconciliation shipped read-only and single-direction
rather than attempting both directions or auto-remediation in one pass.
Active-passive warm standby chosen over cold (too slow) or active-active
(no justified need, and a hard multi-writer-Postgres problem this
codebase has no answer for). A single global external ALB frontend
rather than per-region DNS records, specifically to remove DNS
propagation delay from the RTO critical path. Full list with reasoning:
`docs/high-availability.md` §15, `docs/disaster-recovery.md` §11.

### 16.17 Failure scenarios, narrated

Region A becomes fully unreachable: detected by simultaneous GCLB
backend health-check failure across every Region-A NEG (categorically
different from a single-zone failure, which only fails a subset);
recovery is the manual runbook, target <4h RTO, up to ~1h data loss
bounded by PITR/replication cadence. A mass-deletion incident
(compromised credential or application bug) is *not* a regional outage
but sits in the DR document because its recovery mechanism is DR's, not
HA's: PITR to just before the deletion, then a reconciliation pass
against the restored state to catch any GCS-side drift the restore alone
didn't fix. Full narration: `docs/disaster-recovery.md` §12.

### 16.18 Completion checklist

- [x] HA vs. DR distinction stated and held to throughout (§16.1)
- [x] Availability target (99.9%) and RTO/RPO (<4h / <1h) chosen and justified, not asserted (§16.2–16.3)
- [x] Multi-zone GKE: `topologySpreadConstraints` on API + all 4 workers, 2 workers bumped to 2 replicas, worker PDBs added (§16.4)
- [x] Cloud SQL HA / Memorystore HA designed; existing Phase 7 in-app degradation confirmed IMPLEMENTED+TESTED (§16.5)
- [x] Pub/Sub/worker resilience confirmed unchanged-and-sufficient from Phase 8, replica bump applied (§16.6)
- [x] Reconciliation: designed, implemented, tested, read-only, single-direction, documented gap for the other direction (§16.7)
- [x] Multi-region DR design with a runbook, DNS-failover caveats, secrets/IAM-for-DR guidance (§16.8)
- [x] GCS durability/protection strategy analyzed with a cost-aware recommendation, not the most expensive default (§16.9)
- [x] Failure matrix, monitoring metric inventory, severity-tiered alerts, security-during-failover review (§16.10)
- [x] Cost comparison across single-zone/multi-zone/multi-region, no fabricated pricing (§16.11)
- [x] Chaos-testing procedures for all 13 requested scenarios, environment-labeled (§16.12)
- [x] New settings additive and defaulted, mirrored into the ConfigMap (§16.13)
- [x] 6 new tests, 416/416 total, zero regressions (§16.14)
- [ ] Any HA/DR claim in this section MEASURED against real GCP infrastructure — **not done this session**, see `docs/high-availability.md` §16 and `docs/disaster-recovery.md` §14 for the exact procedure

## 17. Installation

```bash
git clone <repo-url> nimbusfs && cd nimbusfs
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit secrets, especially JWT_SECRET_KEY
```

## 18. Environment Variables

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
| `BUILD_VERSION` / `GIT_COMMIT` | Baked in at container build time; surfaced in logs + health endpoints (Phase 4) |
| `TRUSTED_PROXIES` | Comma-separated proxy IPs whose `X-Forwarded-*` headers are honored; `*` trusts any (Phase 4) |
| `IDEMPOTENCY_KEY_TTL_SECONDS` | How long a completed `Idempotency-Key` response is replayable (Phase 4) |
| `LOCK_DEFAULT_TTL_SECONDS` | Default TTL for Redis-backed distributed locks (Phase 4) |
| `RETRY_MAX_ATTEMPTS` / `RETRY_BASE_DELAY_SECONDS` / `RETRY_MAX_DELAY_SECONDS` | Backoff policy for transient DB/Redis/Storage failures (Phase 4) |
| `FAIL_FAST_ON_STARTUP` | Refuse to finish startup if a critical dependency is unreachable after retrying (Phase 4) |
| `SHUTDOWN_GRACE_PERIOD_SECONDS` | Ceiling on graceful shutdown; match/exceed with uvicorn's `--timeout-graceful-shutdown` (Phase 4) |
| `CHUNK_MIN_SIZE_BYTES` / `CHUNK_MAX_SIZE_BYTES` / `CHUNK_DEFAULT_SIZE_BYTES` | Allowed chunk-size range for `/uploads/*`; max also bounds per-chunk in-memory buffering (Phase 6) |
| `MAX_CHUNKS_PER_UPLOAD` | Sanity ceiling on chunk count per upload session; also bounds GCS multi-stage Compose recursion depth (Phase 6) |
| `MAX_CHUNKED_UPLOAD_SIZE_GB` | Hard cap on declared total file size for the chunked-upload path — separate from, and larger than, `MAX_UPLOAD_SIZE_MB` (Phase 6) |
| `UPLOAD_SESSION_EXPIRATION_MINUTES` | How long an idle upload session survives before lazy expiration (Phase 6) |
| `CACHE_ENABLED` | Master cache kill switch; `false` makes the app behave exactly as it did in Phases 1-6 (Postgres-only) (Phase 7) |
| `CACHE_KEY_PREFIX` | Global key namespace, so a shared Redis can be co-tenanted and `SCAN nimbusfs:*` is a full inventory (Phase 7) |
| `CACHE_TTL_*_SECONDS` | Per-entity TTLs (user/folder/children/breadcrumbs/file/versions/search) — the hard ceiling on staleness if invalidation is ever missed (Phase 7) |
| `CACHE_MAX_VALUE_BYTES` / `CACHE_SEARCH_MAX_ITEMS` | Refuse to cache oversized values / oversized search pages, so one huge entry cannot evict the hot working set (Phase 7) |
| `CACHE_STAMPEDE_*` | Single-flight thundering-herd protection: lock TTL, bounded follower wait, poll interval (Phase 7) |
| `CACHE_WRITE_GUARD_SECONDS` | Opt-in post-invalidation tombstone closing the invalidate-before-commit race; `0` = off (Phase 7) |
| `REDIS_SOCKET_*_TIMEOUT_SECONDS` / `REDIS_RETRY_ON_TIMEOUT` / `REDIS_HEALTH_CHECK_INTERVAL_SECONDS` | Redis pool timeouts — deliberately tighter than DB/GCS, because a *slow* cache is worse than an absent one (Phase 7) |
| `RATE_LIMIT_ENABLED` | Master rate-limit switch (Phase 7) |
| `RATE_LIMIT_FAIL_OPEN` | `true` (default): Redis unreachable => allow + log loudly. `false` fails closed with 429 (Phase 7) |
| `RATE_LIMIT_<CATEGORY>_REQUESTS` / `_WINDOW_SECONDS` | Per-category token-bucket budgets: login, register, metadata, search, upload_initiate, upload_complete, default (Phase 7) |
| `PUBSUB_ENABLED` | Master event kill switch. **Defaults to `false`** so the integration lands dark: outbox rows are still written transactionally, they just never leave Postgres (Phase 8) |
| `GCP_PROJECT_ID` | Project owning the topics/subscriptions — deliberately separate from `GCS_PROJECT_ID`, since bytes and events need not co-locate (Phase 8) |
| `PUBSUB_EMULATOR_HOST` | Point the client library at a local emulator; **never set this in a real cluster** (Phase 8) |
| `FILE_EVENTS_TOPIC` / `UPLOAD_EVENTS_TOPIC` / `NOTIFICATION_EVENTS_TOPIC` | The three domain topics — one per genuine fan-out boundary (Phase 8) |
| `FILE_WORKER_SUBSCRIPTION` / `THUMBNAIL_WORKER_SUBSCRIPTION` / `NOTIFICATION_WORKER_SUBSCRIPTION` | One subscription per worker, `{topic}-{consumer}-sub` — separate settings because each is scaled and DLQ-routed on its own (Phase 8) |
| `MAX_DELIVERY_ATTEMPTS` | Attempts before Pub/Sub dead-letters. The real counter lives on the subscription in GCP; this mirrors it for logging (Phase 8) |
| `PUBSUB_ACK_DEADLINE` | Must exceed the p99 of the slowest consumer (thumbnailing) or Pub/Sub redelivers work still in flight (Phase 8) |
| `OUTBOX_BATCH_SIZE` / `OUTBOX_POLL_INTERVAL` | Publisher loop sizing (Phase 8) |
| `OUTBOX_RETRY_BASE_DELAY_SECONDS` / `OUTBOX_RETRY_MAX_DELAY_SECONDS` | Exponential backoff for a failed row: `min(BASE * 2**(attempt-1), MAX)`. Without it a Pub/Sub outage becomes a self-inflicted retry storm (Phase 8) |
| `WORKER_CONCURRENCY` | Pub/Sub `FlowControl(max_messages)` — the real backpressure knob. Lowered to 3 for the thumbnail worker, whose limit is RAM, not network waits (Phase 8) |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` / `WORKER_HEARTBEAT_FILE_PATH` | Liveness-probe target, touched on a timer independent of message arrival — an idle worker is healthy, not dead (Phase 8) |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | Bounded drain on SIGTERM; keep below the Deployment's `terminationGracePeriodSeconds` (Phase 8) |
| `THUMBNAIL_MAX_DIMENSION_PX` | Bounding box; aspect ratio preserved, small images never upscaled (Phase 8) |
| `THUMBNAIL_SUPPORTED_CONTENT_TYPES` | Explicit allow-list, enforced **before** any download or decode — Pillow is never handed bytes of an unlisted type (Phase 8) |
| `THUMBNAIL_OBJECT_PREFIX` | GCS prefix for generated thumbnails; the object name is deterministic so regeneration overwrites rather than orphans (Phase 8) |
| `RECONCILIATION_ENABLED` | Master switch for `python -m app.workers.reconciliation_job`; `false` makes the scheduled run a no-op (Phase 9) |
| `RECONCILIATION_DRY_RUN` | Documented seam for a future apply-mode — there is no delete/mutate code path in the reconciliation service regardless of this flag's value today (Phase 9) |
| `RECONCILIATION_BATCH_SIZE` | Keyset-pagination page size when walking `FileMetadata` rows — bounds memory, never uses `OFFSET` (Phase 9) |
| `RECONCILIATION_MAX_ISSUES` | Hard ceiling on issues fetched per run, so a badly-corrupted dataset can't turn a scheduled CronJob into an unbounded GCS API bill (Phase 9) |

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

## 19. Running Locally (without Docker)

Requires a local PostgreSQL and Redis instance matching your `.env`.

```bash
alembic upgrade head
./scripts/run_dev.sh
# or: uvicorn app.main:app --reload
```

API available at `http://localhost:8000`, docs at `http://localhost:8000/docs`.

## 20. Running with Docker

```bash
cp .env.example .env
docker compose up --build
```

This starts the API, PostgreSQL, and Redis with health checks and a shared
network. Apply migrations inside the running container:

```bash
docker compose exec api alembic upgrade head
```

## 21. Database Migrations (Alembic)

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
- `0004_chunked_uploads` — creates `upload_sessions`, `upload_chunks`
  (Phase 6)

## 22. API Documentation

- Swagger UI: `GET /docs`
- ReDoc: `GET /redoc`
- Raw OpenAPI schema: `GET /openapi.json`

(Docs are automatically disabled when `ENVIRONMENT=production`.)

## 23. Testing

Tests run against an isolated in-memory SQLite database — no external
services required. **410/410 passing** (145 from Phases 1-6, 101 added by
Phase 7, 164 added by Phase 8).

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
- **Phase 4** (`tests/test_distributed.py` + additions to
  `tests/test_health.py`, against an in-memory `FakeRedisClient` — see
  `tests/fakes/fake_redis.py`, never real Redis): idempotency-key replay
  on retry, idempotency-key reused with a different payload (422),
  concurrent requests sharing one idempotency key (never two uploads),
  the `IdempotencyService` unit contract (proceed/replay/fail), distributed
  lock acquire/release/conflict and token-safety (a released lock never
  clobbers a different holder's re-acquired one), retry-with-backoff
  (transient success, exhaustion, non-retryable exceptions skip retry),
  circuit breaker state transitions (closed -> open -> half-open),
  `/health`/`/ready`/`/live` response shape, correlation/trace/server-ID
  headers and their propagation from an inbound `X-Correlation-ID`,
  graceful degradation (a dependency check that itself raises still
  returns a structured `200`/`503`, never a `500`), and many-concurrent-
  requests-get-unique-IDs as a statelessness sanity check. Note:
  `/health`/`/ready` intentionally check *real* DB/Redis connectivity
  (not the SQLite/fake overrides) — see `tests/conftest.py` for how the
  suite keeps that fast and deterministic without requiring real infra.
- **Phase 6** (`tests/test_chunked_upload.py`, 41 tests, against
  `FakeGCSClient` + `FakeBlob.compose()` + `FakeRedisClient`): initiate,
  first/multiple/out-of-order chunk upload, safe chunk retry (identical
  content, no-op) vs. real overwrite (different content), invalid chunk
  (wrong size, out-of-range number, over the server's bounded-read cap,
  checksum mismatch), missing-chunk completion rejection, resume
  (partial upload -> correct `missing_chunks` -> complete), lazy
  expiration, cancellation (idempotent, temp-object cleanup, blocked
  once completed), byte-exact completion + download round-trip,
  temp-object cleanup after Compose, duplicate/idempotent completion
  requests (with and without `Idempotency-Key`), concurrent completion
  (never two files), ownership scoping (404, not 403), invalid state
  transitions, simulated database/GCS/Redis failures (503/502/503,
  never a raw leaked exception), large declared file size, invalid file
  size, and the chunk-count ceiling.
- **Phase 7** (`tests/test_caching.py` 72 tests + `tests/test_rate_limiting.py`
  29 tests, against the *extended* `FakeRedisClient` — hashes, `SCAN`,
  `INCR`, `EXPIRE`, `TTL`, real token-bucket arithmetic, a controllable
  clock, and **failure injection**, never real Redis): key naming,
  cross-entity collision safety and fingerprint stability; serializer
  round-tripping of datetime/UUID/Decimal/Enum/set/BaseModel, unknown
  schema version treated as a miss, refusal to cache raw bytes;
  get/set/delete/exists/expire/increment, TTL lapse via the controllable
  clock, oversized-value refusal, and the disabled-cache no-op; cache-aside
  population plus the **50-concurrent-request stampede assertion** (fewer
  than 10 may reach the source) and stampede-lock release when the loader
  raises; graceful degradation on *every* cache operation with Redis
  injected-failing, including "Redis dies mid-request"; invalidator
  fan-out per operation and per-user search scoping; the opt-in write
  guard; lock acquisition, contention, self-expiry, ownership, the
  lost-lock Lua-release guard, strict release, bounded-timeout acquire,
  and "Redis unreachable is never mistaken for a free lock"; rate limits
  within and over budget, an exact `Retry-After`, refill over controlled
  time, capacity capping (no banking), **20 concurrent requests sharing
  ONE bucket**, identity and category isolation, reset/peek, fail-open and
  fail-closed, and forged-token IP fallback; and end-to-end cache/DB
  consistency through the real HTTP API across
  create/read/rename/move/trash/restore/delete/version/search, a
  cross-user **404** authorization test proving the cache is not an
  authorization bypass, and a full "Redis is dead, the API keeps working"
  test.

- **Phase 8** (11 files, 164 tests, against `FakePubSubClient` +
  `FakeGCSClient` + in-memory SQLite, never real Pub/Sub): envelope field
  defaults and JSON round-tripping, every `EventType` having a topic
  route; publish-when-enabled, no-op-when-disabled, and the
  `concurrent.futures.Future` → asyncio bridge; outbox
  `fetch_pending_batch` ordering, `mark_published`/`mark_failed`
  transitions and the exponential-backoff arithmetic; **all nine emission
  hook points** producing exactly one row with the right
  aggregate/payload, and services constructed without an outbox emitting
  nothing; publisher polling, per-row commit, failure marking a row
  `FAILED` with an incremented attempt count and a `FAILED` row past its
  `next_attempt_at` being retried; the complete `BaseWorker` ack policy —
  ack-after-success, nack-on-retryable, ack-plus-`FAILED`-on-permanent,
  duplicate pre-check skip, and a lost idempotency race still acking;
  GCS-object validation, fan-out only for supported image types, and the
  **deterministic derived event ID** test that exists to catch the
  dedup-defeating `uuid4()` regression; **real Pillow on real bytes** for
  all four supported formats plus truncated files, empty objects, a PNG
  mislabelled as a JPEG, and the proof that an unsupported type is
  rejected before storage is even touched; notification rendering,
  fallback templates, and the stub sender's flush-never-commit contract;
  and `tests/test_events_integration.py`, which drives an image upload
  through the real HTTP API and then every stage of the chain in turn —
  outbox row → published message → fan-out → rendered thumbnail →
  notification row — plus a full-chain redelivery proving nothing
  duplicates, and a mid-flight Pub/Sub outage proving the event stays
  durable and replayable.

Total: **416 tests passing** (57 Phase 1/2 + 19 Phase 3 + 28 Phase 4 +
41 Phase 6 + 101 Phase 7 + 164 Phase 8 + 6 Phase 9 — Phase 5 shipped
infrastructure/manifests, not application tests). Zero regressions: all
410 pre-Phase-9 tests still pass unchanged.

## 24. Future Roadmap (Phases 10–15, not yet built)

Sharing & permissions between users, virus scanning integration,
full-text content search, content-dedup extension to the chunked-upload
path, CI/CD via GitHub Actions, Terraform IaC, Cloud Armor, Cloud CDN,
observability (Cloud Monitoring/Logging dashboards, OpenTelemetry
tracing). Kubernetes/GKE deployment and autoscaling (HPA) shipped in
Phase 5 (§12); chunked/resumable uploads shipped in Phase 6 (§13); real
rate limiting and Redis metadata caching shipped in Phase 7 (§14);
Pub/Sub-driven background workers and thumbnail generation shipped in
Phase 8 (§15); **multi-zone high availability, disaster recovery design,
and read-only Postgres↔GCS reconciliation shipped in Phase 9 (§16)** —
all previously listed here.

Three things stay on this list even though Phase 9 made some of them
easier to eventually build: reconciliation of stuck `COMPLETING`-state
upload sessions (Phase 6's known gap, distinct from Phase 9's
Postgres↔GCS drift reconciliation — the workers that could run it exist
since Phase 8, but no such job was written), backlog-based worker
autoscaling, and orphaned-GCS-object detection (the direction Phase 9's
reconciliation job deliberately does not cover — see
`docs/disaster-recovery.md` §5.2).

Phase 7's own known gaps (post-commit invalidation, descendant-breadcrumb
invalidation fan-out, negative caching, probabilistic early expiration,
cache warming, a metrics backend) are catalogued in §14.15 and
`docs/PHASE_7_REDIS_DESIGN.md`. Phase 8's are in §15.16 and
`docs/event-driven-architecture.md` — chief among them that **no part of
Phase 8 was ever run against real infrastructure.** Phase 9's are in
§16.18 and `docs/high-availability.md`/`docs/disaster-recovery.md`/
`docs/backup-restore.md` — chief among them that **no HA/DR claim in
Phase 9 has been MEASURED against real infrastructure either; every
number is a justified target, not a drill result.**

## 24a. Phase 11 — Observability, Monitoring, Distributed Tracing & Alerting

A repository/security-audit-first pass (per the Phase 11 brief) found
NimbusFS's observability foundation already substantially real, built
incrementally since Phase 4: structured JSON logging (`structlog`),
correlation/trace/server IDs bound via `structlog.contextvars` and
propagated through the Phase 8 outbox/Pub/Sub/worker chain, and
correctly-separated `/live`/`/ready`/`/health` endpoints. What was
missing — a `/metrics` endpoint (explicitly deferred by Phase 7's own
docstrings and flagged as a "placeholder ... future phase" in
`k8s/07-deployment.yaml`'s annotation), any metrics library, any
span/tracing primitive, and `trace_id` propagation across the Pub/Sub
hop — is what this phase adds:

- **Metrics** (`app/core/metrics.py`, `prometheus_client`): bounded-
  cardinality RED/golden-signal counters/histograms/gauges for HTTP,
  auth, file uploads/downloads, chunked uploads, the DB connection
  pool, cache operations, rate-limit decisions, Pub/Sub publish/process,
  and worker jobs — exposed at `GET /metrics`
  (`app/api/observability_routes.py`, unversioned, unauthenticated by
  design, access-controlled at the network layer). Scraped by **Google
  Managed Prometheus**, not a self-hosted Prometheus/Grafana deployment
  — see `docs/monitoring.md` §1 for the explicit comparison the Phase
  11 brief requires before choosing, and `k8s/24-podmonitoring.yaml`
  for the `PodMonitoring` resource that wires up the scrape.
- **Lightweight distributed tracing** (`app/core/tracing.py`): a
  `start_span()` context manager producing nested, timed
  `span_started`/`span_completed`/`span_failed` structured log events
  (not a full OpenTelemetry SDK integration — `docs/observability.md`
  §5 spells out exactly why, and the mechanical migration path if one
  is added later), wired around `StorageService`'s GCS calls.
  `EventEnvelope` gained a `trace_id` field, populated from the same
  `structlog.contextvars` `correlation_id` already reads
  (`app/events/emitter.py`) and rebound into a worker's own logging
  context on consumption (`app/workers/base.py::_handle`) — closing the
  one real gap in Phase 8's already-good correlation/causation chain:
  an engineer can now grep logs for one `X-Trace-ID` across the HTTP
  request, the outbox publish, and the worker's execution.
- **Logging hardened**: `app/logging/logger.py` gained
  `_redact_sensitive_fields`, a `structlog` processor that redacts any
  field bound under a fixed sensitive-key set
  (`password`/`access_token`/`refresh_token`/`authorization`/`signed_url`/
  etc.) — defense-in-depth on top of a security audit that found no
  actual secret-logging call site in the existing codebase (documented
  in full in `docs/observability.md` §2).
- **Health endpoints reviewed, unchanged** — liveness/readiness
  separation (§22 of the brief) was already correct; verified, not
  rebuilt.
- **Documentation**: `docs/observability.md` (architecture, security
  audit, logs/metrics/traces, testing, remaining risks),
  `docs/monitoring.md` (metric inventory, Cloud Monitoring vs.
  Prometheus/Grafana comparison, dashboards, load-testing status),
  `docs/alerting.md` (the full CRITICAL/HIGH/MEDIUM alert catalog +
  `terraform/monitoring.tf`), `docs/slo.md` (SLIs/SLOs/error budget,
  explicitly unmeasured against real traffic), `docs/incident-response.md`
  (investigation workflows + an honest failure-detection matrix,
  including the one real gap found: no dead-letter-queue metric/replay
  tooling exists yet).
- **Testing**: `tests/test_observability.py` (20 new tests — redaction,
  metrics shape/cardinality/endpoint, span nesting/propagation,
  envelope `trace_id` round-trip and end-to-end HTTP propagation).
  **449/449 tests passing** (429 pre-Phase-11 + 20 new), zero
  regressions.
- **Nothing in this phase was run against real infrastructure** — no
  real GKE cluster, Cloud Monitoring project, or production traffic
  existed this session, so every SLO/alert-threshold/dashboard is
  DESIGNED (and, for the logging/metrics/tracing mechanisms themselves,
  IMPLEMENTED + TESTED against fakes), never MEASURED — see
  `docs/observability.md`'s opening section for the full
  DESIGNED/IMPLEMENTED/TESTED/MEASURED discipline this phase (and every
  phase since 9) holds itself to.

## 25. Contribution Guide

1. Create a feature branch from `main`.
2. Keep business logic in `services/`, persistence in `repositories/` — never
   in route handlers.
3. Add/extend tests for any behavior change; run `pytest` before opening a PR.
4. Run `alembic revision --autogenerate` for any model change and commit the
   generated migration alongside the model change.
5. Follow existing typing/async/PEP8 conventions.