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

from functools import lru_cache

from google.cloud import storage

from app.core.config import get_settings

settings = get_settings()


@lru_cache
def get_storage_client() -> storage.Client:
    """Return a process-wide singleton GCS client."""
    if settings.GCS_CREDENTIALS_PATH:
        return storage.Client.from_service_account_json(
            settings.GCS_CREDENTIALS_PATH, project=settings.GCS_PROJECT_ID
        )
    return storage.Client(project=settings.GCS_PROJECT_ID)
