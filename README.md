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
background workers, a monitoring/observability stack, CI/CD automation,
multi-region deployment, and disaster recovery are all explicitly out
of scope — see §22 "Future Roadmap". (Chunked uploads — listed here as
out-of-scope when this section was originally written — shipped in
Phase 6, §13.)

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

## 14. Installation

```bash
git clone <repo-url> nimbusfs && cd nimbusfs
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit secrets, especially JWT_SECRET_KEY
```

## 15. Environment Variables

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

## 17. Running Locally (without Docker)

Requires a local PostgreSQL and Redis instance matching your `.env`.

```bash
alembic upgrade head
./scripts/run_dev.sh
# or: uvicorn app.main:app --reload
```

API available at `http://localhost:8000`, docs at `http://localhost:8000/docs`.

## 18. Running with Docker

```bash
cp .env.example .env
docker compose up --build
```

This starts the API, PostgreSQL, and Redis with health checks and a shared
network. Apply migrations inside the running container:

```bash
docker compose exec api alembic upgrade head
```

## 19. Database Migrations (Alembic)

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

## 20. API Documentation

- Swagger UI: `GET /docs`
- ReDoc: `GET /redoc`
- Raw OpenAPI schema: `GET /openapi.json`

(Docs are automatically disabled when `ENVIRONMENT=production`.)

## 21. Testing

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

Total: **145 tests passing** (57 Phase 1/2 + 19 Phase 3 + 28 Phase 4 +
41 Phase 6 — Phase 5 shipped infrastructure/manifests, not application
tests).

## 22. Future Roadmap (Phases 7–15, not yet built)

Sharing & permissions between users, virus scanning integration,
thumbnail generation, full-text content search, Pub/Sub-driven
background workers (including reconciliation of stuck
`COMPLETING`-state upload sessions — see Phase 6 §13's "Advanced"
interview question), real rate limiting, Redis metadata caching,
content-dedup extension to the chunked-upload path, CI/CD via GitHub
Actions, Terraform IaC, Cloud Armor, Cloud CDN, observability (Cloud
Monitoring/Logging dashboards, OpenTelemetry tracing), multi-region
deployment, disaster recovery. Kubernetes/GKE deployment and
autoscaling (HPA) shipped in Phase 5 (§12); chunked/resumable uploads
shipped in Phase 6 (§13) — both previously listed here.

## 23. Contribution Guide

1. Create a feature branch from `main`.
2. Keep business logic in `services/`, persistence in `repositories/` — never
   in route handlers.
3. Add/extend tests for any behavior change; run `pytest` before opening a PR.
4. Run `alembic revision --autogenerate` for any model change and commit the
   generated migration alongside the model change.
5. Follow existing typing/async/PEP8 conventions.