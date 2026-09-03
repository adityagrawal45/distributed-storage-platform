"""
Application configuration using Pydantic Settings.

Design decisions:
- A single `Settings` class reads from environment variables / .env file.
- `Environment` enum drives environment-specific behavior (docs exposure,
  log level, cookie security flags) without branching all over the code.
- Secrets (DB password, JWT secret) have NO default in production; they
  must be supplied via environment variables. Sensible dev defaults are
  provided purely for local `docker-compose` convenience.
- `lru_cache` on `get_settings()` ensures settings are parsed once per
  process (cheap, immutable, safe to share across async requests).
"""

import socket
import uuid
from enum import Enum
from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    """Deployment environment. Drives conditional behavior across the app."""

    DEVELOPMENT = "development"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """
    Centralized, typed application configuration.

    All values can be overridden via environment variables or a `.env`
    file. Never hardcode secrets here — this class only defines shape
    and (non-sensitive) defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    APP_NAME: str = "NimbusFS"
    APP_VERSION: str = "0.1.0"
    APP_DESCRIPTION: str = (
        "A Cloud-Native Distributed File Storage Platform built with "
        "Python, FastAPI and Google Cloud."
    )
    ENVIRONMENT: Environment = Environment.DEVELOPMENT
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # ------------------------------------------------------------------
    # Server
    # ------------------------------------------------------------------
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # ------------------------------------------------------------------
    # Security / JWT
    # ------------------------------------------------------------------
    # No default secret in real deployments — must be injected via env/secret
    # manager. A dev-only fallback is provided so `docker-compose up` works
    # out of the box for local development.
    JWT_SECRET_KEY: str = Field(default="CHANGE_ME_DEV_ONLY_SECRET_KEY")
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    JWT_ISSUER: str = "nimbusfs"

    # ------------------------------------------------------------------
    # CORS / Security Headers
    # ------------------------------------------------------------------
    # NOTE: these are declared as plain `str` (not `List[str]`), because
    # pydantic-settings attempts to JSON-decode any env var mapped to a
    # `list`-typed field *before* any field_validator runs. A plain
    # comma-separated value like "http://localhost:3000" is not valid
    # JSON, so that eager decode raises before our own parsing logic
    # ever executes. Storing the raw string and exposing a computed
    # `List[str]` property below sidesteps that entirely.
    CORS_ALLOWED_ORIGINS_RAW: str = Field(default="http://localhost:3000", alias="CORS_ALLOWED_ORIGINS")
    ALLOWED_HOSTS_RAW: str = Field(default="*", alias="ALLOWED_HOSTS")

    # ------------------------------------------------------------------
    # PostgreSQL
    # ------------------------------------------------------------------
    POSTGRES_USER: str = "nimbusfs"
    POSTGRES_PASSWORD: str = "nimbusfs"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "nimbusfs"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20
    DATABASE_ECHO: bool = False

    # ------------------------------------------------------------------
    # Redis
    # ------------------------------------------------------------------
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
    REDIS_MAX_CONNECTIONS: int = 20
    # Phase 7: explicit socket timeouts on the shared pool. A hung Redis
    # call must fail fast so a request can degrade to Postgres instead of
    # pinning an event-loop task forever. These are deliberately SHORTER
    # than the DB/GCS timeouts: Redis is a cache, and a cache that is slow
    # is worse than a cache that is absent.
    REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS: float = 2.0
    REDIS_SOCKET_TIMEOUT_SECONDS: float = 2.0
    # Retry a dropped connection once at the client-library level. Bounded
    # deliberately — application-level fallback (serve from Postgres) is
    # the real resilience mechanism, not unbounded client retries.
    REDIS_RETRY_ON_TIMEOUT: bool = True
    REDIS_HEALTH_CHECK_INTERVAL_SECONDS: int = 30

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    # ------------------------------------------------------------------
    # Server / Instance Identity (Phase 4)
    # ------------------------------------------------------------------
    # `INSTANCE_ID` identifies *this process* across a fleet of
    # interchangeable replicas — critical once >1 FastAPI instance is
    # running behind a load balancer. It is deliberately random and
    # generated once per process (not persisted), never configured by an
    # operator: on Cloud Run/GKE every pod restart should get a fresh
    # identity. `HOSTNAME` defaults to the OS hostname, which on
    # Cloud Run/GKE is the container/pod name — useful for correlating
    # logs back to a specific replica in the platform's own console.
    INSTANCE_ID: str = Field(default_factory=lambda: str(uuid.uuid4()))
    HOSTNAME: str = Field(default_factory=lambda: socket.gethostname())
    # `BUILD_VERSION`/`GIT_COMMIT` are baked into the container image at
    # build time (e.g. `docker build --build-arg GIT_COMMIT=$(git rev-parse
    # HEAD)`) and surfaced via env vars — NOT computed at runtime, so a
    # running process can prove exactly which build it's serving.
    BUILD_VERSION: str = Field(default="dev-local")
    GIT_COMMIT: str = Field(default="unknown")

    # ------------------------------------------------------------------
    # Distributed Backend (Phase 4)
    # ------------------------------------------------------------------
    # Trusted reverse proxies (load balancer / Cloud Run front end) whose
    # `X-Forwarded-*` headers we honor. "*" trusts any proxy — fine
    # behind a locked-down Cloud LB where the app is never internet-
    # reachable directly, but should be pinned to real proxy CIDRs/IPs
    # once that topology is known.
    TRUSTED_PROXIES_RAW: str = Field(default="*", alias="TRUSTED_PROXIES")

    # Idempotency-Key support (see app/services/idempotency_service.py).
    IDEMPOTENCY_KEY_TTL_SECONDS: int = 86400  # 24h — long enough to absorb client retry storms
    IDEMPOTENCY_LOCK_TIMEOUT_SECONDS: int = 60  # generous ceiling on "how long can one upload take"

    # Distributed lock defaults (app/core/distributed_lock.py).
    LOCK_DEFAULT_TTL_SECONDS: int = 30
    LOCK_ACQUIRE_TIMEOUT_SECONDS: float = 5.0
    LOCK_RETRY_INTERVAL_SECONDS: float = 0.1

    # Retry policy applied to transient infra failures (DB/Redis/GCS)
    # during startup and on the request path — see app/core/retry.py.
    RETRY_MAX_ATTEMPTS: int = 3
    RETRY_BASE_DELAY_SECONDS: float = 0.2
    RETRY_MAX_DELAY_SECONDS: float = 2.0

    # Startup fails fast (process exits) if critical dependencies are
    # unreachable after retrying — this is intentional: a Kubernetes/Cloud
    # Run scheduler should not route traffic to a replica that can never
    # serve a request, and a crash-looping pod is a much clearer signal
    # than one silently serving 500s. Set to False only for offline
    # tooling (e.g. `alembic` invocations) that import the app without
    # ever running the ASGI lifespan.
    FAIL_FAST_ON_STARTUP: bool = True

    # Graceful shutdown: how long to wait for in-flight requests to
    # finish before forcibly closing pools. Uvicorn's own
    # `--timeout-graceful-shutdown` should be set to a value >= this.
    SHUTDOWN_GRACE_PERIOD_SECONDS: float = 10.0

    @property
    def TRUSTED_PROXIES(self) -> List[str]:
        return [item.strip() for item in self.TRUSTED_PROXIES_RAW.split(",") if item.strip()]

    @property
    def trust_any_proxy(self) -> bool:
        return self.TRUSTED_PROXIES_RAW.strip() == "*"

    # ------------------------------------------------------------------
    # Google Cloud Storage (Phase 3)
    # ------------------------------------------------------------------
    GCS_PROJECT_ID: str = "nimbusfs-dev"
    GCS_BUCKET_NAME: str = "nimbusfs-files-dev"
    # Path to a service-account JSON key file. Left unset in
    # staging/production, where Application Default Credentials (a
    # workload-identity-bound service account on GKE/Cloud Run) are used
    # instead — never ship a key file inside a container image.
    GCS_CREDENTIALS_PATH: str | None = None
    SIGNED_URL_EXPIRATION_MINUTES: int = 15
    MAX_UPLOAD_SIZE_MB: int = 100
    # Empty = allow every MIME type except what BLOCKED_EXTENSIONS rejects.
    ALLOWED_MIME_TYPES_RAW: str = Field(default="", alias="ALLOWED_MIME_TYPES")
    # Executable/script extensions are blocked by default — see
    # StorageService design decisions for the rationale.
    BLOCKED_EXTENSIONS_RAW: str = Field(
        default="exe,bat,cmd,msi,dll,com,scr,jar,vbs,ps1,sh,app,apk",
        alias="BLOCKED_EXTENSIONS",
    )

    # ------------------------------------------------------------------
    # Chunked / Resumable Uploads (Phase 6)
    # ------------------------------------------------------------------
    # These govern the LARGE-file path (`/api/v1/uploads/*`), distinct
    # from `MAX_UPLOAD_SIZE_MB` above, which caps the simple single-shot
    # path (`/api/v1/files/upload`, Phase 3) and is deliberately left
    # unchanged — small uploads should stay simple and fast; large ones
    # opt into chunking explicitly.
    CHUNK_MIN_SIZE_BYTES: int = 1 * 1024 * 1024  # 1 MiB — below this, chunking overhead isn't worth it
    CHUNK_MAX_SIZE_BYTES: int = 256 * 1024 * 1024  # 256 MiB — also the per-chunk in-memory buffering ceiling (see ChunkedUploadService)
    CHUNK_DEFAULT_SIZE_BYTES: int = 8 * 1024 * 1024  # 8 MiB — used when a client omits chunk_size at initiate
    MAX_CHUNKS_PER_UPLOAD: int = 10000  # sanity ceiling; also bounds GCS multi-stage compose recursion depth
    MAX_CHUNKED_UPLOAD_SIZE_GB: int = 100
    # How long an upload session may sit idle before it's treated as
    # abandoned. Checked lazily on access (see ChunkedUploadService) —
    # no background sweeper exists yet; that's future-phase work this
    # design deliberately leaves room for (see README Phase 6 section).
    UPLOAD_SESSION_EXPIRATION_MINUTES: int = 120

    # ------------------------------------------------------------------
    # Distributed Caching (Phase 7)
    # ------------------------------------------------------------------
    # Master switch. When false, `CacheService` short-circuits every
    # operation into a miss/no-op — the app behaves exactly as it did in
    # Phases 1-6 (Postgres-only). This is the operational kill switch for
    # "the cache is doing something weird, turn it off and stay up".
    CACHE_ENABLED: bool = True
    # Global key namespace. Every key this app writes starts with it, so a
    # shared Redis instance can host other tenants/apps without collision,
    # and `SCAN nimbusfs:*` is a complete inventory of what we own.
    CACHE_KEY_PREFIX: str = "nimbusfs"

    # Per-entity TTLs. Config-driven, never hardcoded at call sites — see
    # app/core/cache/policy.py for the reasoning behind each default.
    CACHE_TTL_USER_SECONDS: int = 900  # 15 min — user rows change rarely
    CACHE_TTL_FOLDER_SECONDS: int = 300  # 5 min
    CACHE_TTL_FOLDER_CHILDREN_SECONDS: int = 300  # 5 min
    CACHE_TTL_FOLDER_BREADCRUMBS_SECONDS: int = 300  # 5 min
    CACHE_TTL_FILE_SECONDS: int = 300  # 5 min
    CACHE_TTL_FILE_VERSIONS_SECONDS: int = 300  # 5 min
    CACHE_TTL_SEARCH_SECONDS: int = 90  # 1.5 min — search results go stale fastest

    # Refuse to cache anything larger than this. Redis is a shared,
    # memory-resident, single-threaded service: one 50 MB value evicts
    # thousands of useful small ones and blocks the server while it is
    # serialized over the wire. Oversized values are logged and skipped
    # (the request still succeeds, straight from Postgres).
    CACHE_MAX_VALUE_BYTES: int = 256 * 1024  # 256 KiB
    # Ceiling on how many rows a single search page may contain before it
    # is considered "not worth caching" (see SearchService).
    CACHE_SEARCH_MAX_ITEMS: int = 100

    # Cache-stampede (thundering herd) protection — see
    # app/services/cache_service.py::CacheService.get_or_set.
    CACHE_STAMPEDE_PROTECTION_ENABLED: bool = True
    CACHE_STAMPEDE_LOCK_TTL_SECONDS: float = 5.0  # ceiling on how long one populator may hold the right to populate
    CACHE_STAMPEDE_WAIT_SECONDS: float = 0.5  # bounded wait for followers before they read through to Postgres
    CACHE_STAMPEDE_POLL_INTERVAL_SECONDS: float = 0.02

    # Post-invalidation write guard (delayed-double-delete style). When
    # > 0, invalidating a key also plants a short-lived tombstone that
    # suppresses cache *writes* for that key, closing the narrow race
    # where a concurrent reader repopulates the cache with pre-commit data
    # between a writer's DELETE and its COMMIT. Costs one extra round trip
    # per cache population and leaves the key deliberately cold for this
    # many seconds after every write.
    #
    # ON by default as of the Phase 7 follow-up: 1.5s is comfortably above
    # a request transaction's typical commit latency (sub-millisecond to a
    # few ms locally; still well under a second across an AZ on GKE), so it
    # closes the race in the common case while costing at most one extra
    # Postgres read per key per write — cheap next to serving stale data.
    # Set to 0.0 to disable (e.g. if the extra round trip matters more than
    # the race for a given deployment). See docs/PHASE_7_REDIS_DESIGN.md.
    CACHE_WRITE_GUARD_SECONDS: float = 1.5

    # ------------------------------------------------------------------
    # Rate Limiting (Phase 7)
    # ------------------------------------------------------------------
    RATE_LIMIT_ENABLED: bool = True
    # Fail-open (default): if Redis is unreachable, ALLOW the request and
    # log loudly at ERROR. For a file-storage platform, availability beats
    # strict limiting — a Redis outage must not become a total outage.
    # Set false to fail closed (reject with 429) for deployments where
    # abuse protection outranks availability.
    RATE_LIMIT_FAIL_OPEN: bool = True

    # Per-category budgets: `<N> requests per <W> seconds`, enforced as a
    # token bucket of capacity N refilling at N/W tokens per second (so N
    # is also the maximum instantaneous burst).
    RATE_LIMIT_LOGIN_REQUESTS: int = 10
    RATE_LIMIT_LOGIN_WINDOW_SECONDS: int = 60
    RATE_LIMIT_REGISTER_REQUESTS: int = 5
    RATE_LIMIT_REGISTER_WINDOW_SECONDS: int = 300
    # Phase 10: /auth/refresh was previously unmetered — see
    # RateLimitCategory.REFRESH's comment in app/core/rate_limiter.py.
    RATE_LIMIT_REFRESH_REQUESTS: int = 20
    RATE_LIMIT_REFRESH_WINDOW_SECONDS: int = 60
    RATE_LIMIT_METADATA_REQUESTS: int = 300
    RATE_LIMIT_METADATA_WINDOW_SECONDS: int = 60
    RATE_LIMIT_UPLOAD_INITIATE_REQUESTS: int = 60
    RATE_LIMIT_UPLOAD_INITIATE_WINDOW_SECONDS: int = 60
    RATE_LIMIT_UPLOAD_COMPLETE_REQUESTS: int = 60
    RATE_LIMIT_UPLOAD_COMPLETE_WINDOW_SECONDS: int = 60
    RATE_LIMIT_SEARCH_REQUESTS: int = 60
    RATE_LIMIT_SEARCH_WINDOW_SECONDS: int = 60
    RATE_LIMIT_DEFAULT_REQUESTS: int = 600
    RATE_LIMIT_DEFAULT_WINDOW_SECONDS: int = 60

    # ------------------------------------------------------------------
    # Pub/Sub / Event-Driven Architecture (Phase 8)
    # ------------------------------------------------------------------
    # GCP project that owns the topics/subscriptions. Distinct from
    # `GCS_PROJECT_ID` on purpose: nothing forces bytes and events to live
    # in the same project, and pinning them together in config would make
    # splitting them later a code change rather than a config change.
    GCP_PROJECT_ID: str = "nimbusfs-dev"

    # Master kill switch, mirroring `CACHE_ENABLED`'s role for Phase 7.
    # DEFAULT FALSE, deliberately: a brand-new outbound integration should
    # land dark. With it off, `EventPublisher` becomes a logged no-op and
    # the API behaves exactly as it did in Phases 1-7 — outbox rows are
    # still written (they are just Postgres rows in the same transaction),
    # they simply never leave the database until this is flipped on.
    PUBSUB_ENABLED: bool = False
    # When set (e.g. "localhost:8085"), the google-cloud-pubsub client
    # talks to the local emulator instead of real GCP. Exported into the
    # process environment by the publisher/subscriber factories.
    PUBSUB_EMULATOR_HOST: str | None = None

    # Three domain topics, not one firehose and not one-per-event-type:
    # a topic is a fan-out boundary, and these are the three boundaries
    # that actually have distinct consumer sets today.
    FILE_EVENTS_TOPIC: str = "nimbusfs-file-events"
    UPLOAD_EVENTS_TOPIC: str = "nimbusfs-upload-events"
    NOTIFICATION_EVENTS_TOPIC: str = "nimbusfs-notification-events"

    # Per-worker subscriptions, named `{topic}-{consumer}-sub`. Separate
    # settings (not one shared name) because each worker is scaled,
    # ack-deadline-tuned and DLQ-routed independently.
    FILE_WORKER_SUBSCRIPTION: str = "nimbusfs-file-events-file-worker-sub"
    THUMBNAIL_WORKER_SUBSCRIPTION: str = "nimbusfs-file-events-thumbnail-worker-sub"
    NOTIFICATION_WORKER_SUBSCRIPTION: str = "nimbusfs-notification-events-notification-worker-sub"

    # Delivery attempts before Pub/Sub routes a message to the subscription's
    # dead-letter topic. Configured on the SUBSCRIPTION in GCP; mirrored here
    # so workers can log "this is attempt N of M" meaningfully.
    MAX_DELIVERY_ATTEMPTS: int = 5
    # Ack deadline (seconds). Must exceed the p99 processing time of the
    # slowest consumer (thumbnailing) or Pub/Sub redelivers work that is
    # still in flight.
    PUBSUB_ACK_DEADLINE: int = 60

    # Transactional outbox publisher loop.
    OUTBOX_BATCH_SIZE: int = 100
    OUTBOX_POLL_INTERVAL: float = 2.0
    # Exponential backoff applied to a failed outbox row's `next_attempt_at`:
    # delay = min(BASE * 2**(attempt_count - 1), MAX).
    OUTBOX_RETRY_BASE_DELAY_SECONDS: float = 2.0
    OUTBOX_RETRY_MAX_DELAY_SECONDS: float = 300.0

    # Worker runtime.
    WORKER_CONCURRENCY: int = 10
    WORKER_HEARTBEAT_INTERVAL_SECONDS: float = 10.0
    # Liveness-probe target. A background heartbeat task touches this file
    # on a timer INDEPENDENT of message arrival — an idle worker with an
    # empty subscription is healthy, not dead, so a probe keyed on "did we
    # process a message recently" would restart perfectly good pods.
    WORKER_HEARTBEAT_FILE_PATH: str = "/tmp/healthy"
    WORKER_SHUTDOWN_GRACE_SECONDS: float = 30.0

    # Thumbnails (Phase 8).
    THUMBNAIL_MAX_DIMENSION_PX: int = 512
    # Explicit allow-list. Anything outside it is a NonRetryableEventError
    # raised BEFORE any decode is attempted — Pillow must never be handed
    # bytes of an unknown format on the strength of a client-declared MIME
    # type. Same list-env-var technique as CORS_ALLOWED_ORIGINS_RAW.
    THUMBNAIL_SUPPORTED_CONTENT_TYPES_RAW: str = Field(
        default="image/jpeg,image/png,image/webp,image/gif",
        alias="THUMBNAIL_SUPPORTED_CONTENT_TYPES",
    )
    THUMBNAIL_OBJECT_PREFIX: str = "thumbnails"

    # Reconciliation (Phase 9). A read-only consistency check between
    # Postgres (source of truth for metadata) and GCS (source of truth for
    # bytes) — see app/services/reconciliation_service.py and
    # docs/disaster-recovery.md "Reconciliation" for the full design.
    # Report-only in this phase: there is no code path anywhere in the
    # reconciliation job that deletes or mutates a row/object, dry-run or
    # not — RECONCILIATION_DRY_RUN exists as a documented seam for a
    # future apply-mode, not a currently-real switch.
    RECONCILIATION_ENABLED: bool = True
    RECONCILIATION_DRY_RUN: bool = True
    RECONCILIATION_BATCH_SIZE: int = 500
    # Hard ceiling on how many issues a single run will fetch object
    # metadata for, so a badly-corrupted dataset can't turn a scheduled
    # CronJob into an unbounded GCS API bill.
    RECONCILIATION_MAX_ISSUES: int = 5000

    @property
    def THUMBNAIL_SUPPORTED_CONTENT_TYPES(self) -> List[str]:
        return [
            item.strip().lower()
            for item in self.THUMBNAIL_SUPPORTED_CONTENT_TYPES_RAW.split(",")
            if item.strip()
        ]

    @property
    def MAX_CHUNKED_UPLOAD_SIZE_BYTES(self) -> int:
        return self.MAX_CHUNKED_UPLOAD_SIZE_GB * 1024 * 1024 * 1024

    @property
    def MAX_UPLOAD_SIZE_BYTES(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    @property
    def ALLOWED_MIME_TYPES(self) -> List[str]:
        return [item.strip().lower() for item in self.ALLOWED_MIME_TYPES_RAW.split(",") if item.strip()]

    @property
    def BLOCKED_EXTENSIONS(self) -> List[str]:
        return [item.strip().lstrip(".").lower() for item in self.BLOCKED_EXTENSIONS_RAW.split(",") if item.strip()]

    @property
    def CORS_ALLOWED_ORIGINS(self) -> List[str]:
        """Comma-separated env value, e.g. 'http://a.com,http://b.com', as a list."""
        return [item.strip() for item in self.CORS_ALLOWED_ORIGINS_RAW.split(",") if item.strip()]

    @property
    def ALLOWED_HOSTS(self) -> List[str]:
        return [item.strip() for item in self.ALLOWED_HOSTS_RAW.split(",") if item.strip()]

    @property
    def DATABASE_URL(self) -> str:
        """Async SQLAlchemy connection string (asyncpg driver)."""
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def DATABASE_URL_SYNC(self) -> str:
        """Sync SQLAlchemy connection string, used by Alembic migrations."""
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def REDIS_URL(self) -> str:
        """Redis connection string."""
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == Environment.PRODUCTION

    @property
    def docs_enabled(self) -> bool:
        """Disable interactive docs in production for security."""
        return self.ENVIRONMENT != Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    """
    Cached settings accessor.

    Using `lru_cache` means the .env file / environment is parsed exactly
    once per process, and the same immutable `Settings` instance is reused
    everywhere via FastAPI's dependency injection.
    """
    return Settings()