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
import time
from functools import lru_cache

from google.cloud import storage

from app.core.config import get_settings
from app.core.retry import retry_async
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


async def _bucket_exists(client: storage.Client) -> None:
    exists = await asyncio.to_thread(client.bucket(settings.GCS_BUCKET_NAME).exists)
    if not exists:
        raise RuntimeError(f"Bucket '{settings.GCS_BUCKET_NAME}' does not exist or is not accessible.")


async def check_storage_connection(client: storage.Client, *, with_retry: bool = False) -> tuple[bool, float]:
    """
    Verifies the configured bucket is reachable. Used by `/health`/`/ready`
    (`with_retry=False`, fail fast) and by app startup (`with_retry=True`,
    see app/main.py) — startup failing fast here is what satisfies
    "Startup must fail if critical dependencies fail" for storage, not
    just DB/Redis: an instance that can't reach its bucket is as useless
    as one that can't reach Postgres.
    """
    start = time.perf_counter()
    try:
        if with_retry:
            await retry_async(
                lambda: _bucket_exists(client),
                attempts=settings.DEPENDENCY_RETRY_ATTEMPTS,
                base_delay=settings.DEPENDENCY_RETRY_BACKOFF_SECONDS,
                max_delay=settings.DEPENDENCY_RETRY_BACKOFF_MAX_SECONDS,
                retry_on=(Exception,),
                operation_name="gcs_bucket_check",
            )
        else:
            await _bucket_exists(client)
        healthy = True
    except Exception as exc:
        logger.warning("storage_health_check_failed", error=str(exc))
        healthy = False
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    return healthy, latency_ms
