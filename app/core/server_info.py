"""
Server / instance identity (Phase 4).

Design decisions:
- In a distributed deployment every FastAPI process is interchangeable
  from the client's point of view, but for *operators* they are very
  much NOT interchangeable when something goes wrong — "which replica
  served this failing request?" is the first question an on-call
  engineer asks. This module is the single source of truth for
  answering it, consumed by both the logging middleware (every log line)
  and the health endpoints (every health/readiness response).
- Computed once at import time and cached: identity is fixed for the
  lifetime of the process, so there's no reason to recompute it per
  request. `INSTANCE_ID` in particular is generated fresh by
  `Settings` each time the process starts (see
  `app/core/config/settings.py`), which is exactly the semantics we
  want — restart the pod, get a new identity.
"""

import os
from functools import lru_cache

from pydantic import BaseModel

from app.core.config import get_settings


class ServerIdentity(BaseModel):
    """Everything needed to say "this response/log line came from *here*"."""

    instance_id: str
    hostname: str
    process_id: int
    app_version: str
    build_version: str
    git_commit: str
    environment: str


@lru_cache
def get_server_identity() -> ServerIdentity:
    """Process-wide singleton describing the replica currently running."""
    settings = get_settings()
    return ServerIdentity(
        instance_id=settings.INSTANCE_ID,
        hostname=settings.HOSTNAME,
        process_id=os.getpid(),
        app_version=settings.APP_VERSION,
        build_version=settings.BUILD_VERSION,
        git_commit=settings.GIT_COMMIT,
        environment=settings.ENVIRONMENT.value,
    )
