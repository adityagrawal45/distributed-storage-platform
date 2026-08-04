"""
Per-process server identity.

Design decisions:
- In a distributed deployment every FastAPI process must be able to say
  "who am I" — for logs (which pod produced this line?), for health
  checks (which instance answered this probe?), and for response headers
  (`X-Server-ID`) so an operator can correlate a client-reported bug with
  one specific instance's logs.
- Everything here is computed ONCE at import time and never mutated.
  That's intentional: identity is a property of the *process*, not of any
  request, so it belongs in module-level state (not `request.state`, not
  Redis) — this is exactly the kind of local state that's fine to keep in
  memory even in an otherwise-stateless app, because losing it just means
  losing the ability to label logs, never losing or diverging application
  data (see README "Stateless Design" for the full distinction).
- `instance_id` resolution order: explicit `INSTANCE_ID` env var (useful
  for naming replicas in docker-compose) -> `HOSTNAME` (what Kubernetes
  sets to the Pod name automatically, and what Cloud Run sets to a
  per-revision-instance value) -> a random UUID4 as a last resort so the
  field is never empty even on a bare `uvicorn` run.
"""

import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.core.config import get_settings

settings = get_settings()


def _resolve_instance_id() -> str:
    return settings.INSTANCE_ID or os.environ.get("HOSTNAME") or f"local-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class ServerIdentity:
    """Immutable snapshot of this process's identity, computed once at import time."""

    instance_id: str
    hostname: str
    process_id: int
    app_version: str
    build_version: str
    git_commit_sha: str
    environment: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "hostname": self.hostname,
            "process_id": self.process_id,
            "app_version": self.app_version,
            "build_version": self.build_version,
            "git_commit_sha": self.git_commit_sha,
            "environment": self.environment,
            "started_at": self.started_at.isoformat(),
        }


SERVER_IDENTITY = ServerIdentity(
    instance_id=_resolve_instance_id(),
    hostname=socket.gethostname(),
    process_id=os.getpid(),
    app_version=settings.APP_VERSION,
    build_version=settings.BUILD_VERSION,
    git_commit_sha=settings.GIT_COMMIT_SHA,
    environment=settings.ENVIRONMENT.value,
)
