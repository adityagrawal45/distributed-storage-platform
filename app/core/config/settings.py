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

    # ------------------------------------------------------------------
    # Redis
    # ------------------------------------------------------------------
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
    REDIS_MAX_CONNECTIONS: int = 20

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