"""
Google Cloud Storage client factory.

Design decisions:
- Mirrors `app/database/redis.py`: a single client is constructed once
  at import time and reused for the process lifetime. `storage.Client`
  wraps its own internal HTTP connection pooling, so there's no benefit
  to re-authenticating per request — only cost.
- Credential resolution is intentionally environment-driven, not
  hardcoded:
    * If `GCS_CREDENTIALS_PATH` is set (local dev / CI), a service-account
      key file is loaded explicitly.
    * Otherwise (staging/production), Application Default Credentials
      are used — on GKE this resolves to the pod's bound Kubernetes
      service account via Workload Identity, so no key file ever needs
      to exist inside a container image or get mounted as a secret.
  This is what keeps this module compatible with a future Kubernetes
  deployment without any code change, only an infrastructure one.
- The client is a thin, cheap object; the expensive part of GCS calls is
  the network I/O, which `StorageService` runs off the event loop via
  `asyncio.to_thread` (the official `google-cloud-storage` SDK is
  synchronous — there is no supported async client as of this writing).
"""

import asyncio
from functools import lru_cache

from google.cloud import storage

from app.core.config import get_settings
from app.core.retry import RetryExhaustedError, retry_async
from app.logging.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)


@lru_cache
def get_storage_client() -> storage.Client:
    """Return a process-wide singleton GCS client."""
    if settings.GCS_CREDENTIALS_PATH:
        return storage.Client.from_service_account_json(
            settings.GCS_CREDENTIALS_PATH, project=settings.GCS_PROJECT_ID
        )
    return storage.Client(project=settings.GCS_PROJECT_ID)


async def check_storage_connection(client: storage.Client | None = None, *, with_retry: bool = True) -> bool:
    """
    Verifies the configured bucket exists and is reachable.

    Used by `/health` and `/ready` (with retry, tolerating one transient
    blip) and by startup verification (see `app/main.py`'s lifespan,
    which fails fast if this never succeeds — see
    `Settings.FAIL_FAST_ON_STARTUP`). The synchronous `google-cloud-
    storage` SDK call is run off the event loop via `asyncio.to_thread`,
    same pattern as `StorageService`.

    `client` is accepted as a parameter (defaulting to the process-wide
    singleton) rather than always resolved internally, purely so tests
    can pass the same `FakeGCSClient` that overrides
    `app.dependencies.providers.get_gcs_client` elsewhere — this
    function must never make a real network call in the test suite.
    """
    client = client or get_storage_client()

    async def _bucket_exists() -> bool:
        bucket = await asyncio.to_thread(client.bucket, settings.GCS_BUCKET_NAME)
        return await asyncio.to_thread(bucket.exists)

    try:
        if not with_retry:
            return await _bucket_exists()
        try:
            return await retry_async(
                _bucket_exists,
                max_attempts=settings.RETRY_MAX_ATTEMPTS,
                base_delay=settings.RETRY_BASE_DELAY_SECONDS,
                max_delay=settings.RETRY_MAX_DELAY_SECONDS,
                operation_name="gcs_bucket_exists",
            )
        except RetryExhaustedError:
            return False
    except Exception:
        return False
