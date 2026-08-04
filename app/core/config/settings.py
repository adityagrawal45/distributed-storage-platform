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
    # Optional read-replica connection string (async, asyncpg driver).
    # Left unset by default -> `get_db_read()` transparently falls back to
    # the primary engine, so this is pure infrastructure until a later
    # phase actually provisions a replica (see README "Read/Write
    # Separation" for the design).
    DATABASE_READ_REPLICA_URL: str | None = None

    # ------------------------------------------------------------------
    # Redis
    # ------------------------------------------------------------------
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
    REDIS_MAX_CONNECTIONS: int = 20

    # ------------------------------------------------------------------
    # Distributed Backend (Phase 4)
    # ------------------------------------------------------------------
    # Stable identifier for THIS process, exposed in health checks, logs,
    # and the `X-Server-ID` response header so a multi-instance deployment
    # can be debugged ("which pod served this request?"). Normally left
    # unset: Kubernetes/Cloud Run inject a unique hostname per replica
    # automatically (see app.core.server_identity), so an explicit
    # override is only needed for local multi-container demos where the
    # hostname alone isn't descriptive (see docker-compose.yml's app1/
    # app2/app3 services).
    INSTANCE_ID: str | None = None
    # Populated by CI/CD (Docker build args) — see docker/Dockerfile.
    BUILD_VERSION: str = "dev"
    GIT_COMMIT_SHA: str = "unknown"

    # Startup dependency checks (DB/Redis/GCS) retry with exponential
    # backoff before the process fails fast and refuses to start — see
    # app.core.retry and app.main's lifespan.
    DEPENDENCY_RETRY_ATTEMPTS: int = 3
    DEPENDENCY_RETRY_BACKOFF_SECONDS: float = 0.5
    DEPENDENCY_RETRY_BACKOFF_MAX_SECONDS: float = 8.0

    # How long shutdown waits for in-flight requests to finish before
    # closing pools regardless (see app.main's lifespan shutdown path).
    GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS: float = 30.0

    # Idempotency-Key support (see app.middleware.idempotency).
    IDEMPOTENCY_ENABLED: bool = True
    IDEMPOTENCY_KEY_TTL_SECONDS: int = 86400  # 24h — long enough to cover realistic client retry windows

    # Distributed lock default TTL (see app.core.distributed_lock). A lock
    # holder that crashes before releasing still self-heals after this
    # many seconds instead of deadlocking the resource forever.
    DISTRIBUTED_LOCK_DEFAULT_TTL_SECONDS: int = 30

    # Rate limiting is a PLACEHOLDER in Phase 4 — infrastructure only,
    # disabled by default. A later phase decides real limits/policy.
    RATE_LIMIT_ENABLED: bool = False
    RATE_LIMIT_REQUESTS_PER_MINUTE: int = 120

    # Comma-separated list of proxy/load-balancer IPs allowed to set
    # `X-Forwarded-For`/`X-Forwarded-Proto`. Empty (default) = trust
    # nothing but the direct TCP peer. `*` trusts any peer's forwarded
    # headers — convenient for local docker-compose behind nginx, never
    # appropriate in production (see README Security section).
    TRUSTED_PROXIES_RAW: str = Field(default="", alias="TRUSTED_PROXIES")

    # In-process circuit breaker guarding Redis calls (cache/lock/
    # idempotency) — local resilience state, not application state (see
    # app.core.circuit_breaker for why this doesn't violate statelessness).
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
    CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS: float = 30.0

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

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
    def TRUSTED_PROXIES(self) -> List[str]:
        return [item.strip() for item in self.TRUSTED_PROXIES_RAW.split(",") if item.strip()]

    @property
    def DATABASE_URL(self) -> str:
        """Async SQLAlchemy connection string (asyncpg driver)."""
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def DATABASE_URL_READ(self) -> str:
        """Read-replica connection string, falling back to the primary when unset."""
        return self.DATABASE_READ_REPLICA_URL or self.DATABASE_URL

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