"""
Minimal async circuit breaker (Phase 4).

Design decisions:
- Classic three-state machine: CLOSED (normal) -> OPEN (fast-fail,
  stop hammering a dependency that's already down) -> HALF_OPEN (probe
  with a single trial call) -> CLOSED or back to OPEN.
- Per-dependency instances, not global: `StorageService`, DB health
  checks, and Redis each get their own breaker with independent state,
  because GCS being down says nothing about Postgres being down.
- Deliberately in-process/in-memory, NOT shared via Redis. A circuit
  breaker's whole purpose is to protect *this process's* outbound calls
  from wasting time on a dependency that's failing; coordinating that
  state across replicas would add a Redis round-trip to every call,
  defeating the "fail fast" point. Each replica opens its own breaker
  independently — that's fine, they all observe the same failing
  dependency and converge to the same state within one failure window.
- This module only provides the primitive. Phase 4 wires it into the
  health-check paths (see `app/api/v1/health/routes.py`) to demonstrate
  the pattern; broader application (e.g. wrapping every StorageService
  call) is deliberately left to whichever future phase needs it, per
  the "prepare infrastructure only" instruction for this phase.
"""

import time
from enum import Enum

from app.exceptions.custom_exceptions import CircuitBreakerOpenException
from app.logging.logger import get_logger

logger = get_logger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """
    Wraps calls to a single downstream dependency.

    Usage:
        breaker = CircuitBreaker(name="gcs", failure_threshold=5, recovery_timeout=30)
        result = await breaker.call(some_async_callable)
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    async def call(self, operation):
        """Runs `operation()` (a zero-arg async callable) through the breaker."""
        current_state = self.state

        if current_state == CircuitState.OPEN:
            raise CircuitBreakerOpenException(
                detail=f"Circuit breaker '{self.name}' is open; dependency assumed unavailable."
            )

        try:
            result = await operation()
        except Exception:
            self._on_failure()
            raise
        else:
            self._on_success()
            return result

    def _on_success(self) -> None:
        if self._state != CircuitState.CLOSED:
            logger.info("circuit_breaker_closed", breaker=self.name)
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = None

    def _on_failure(self) -> None:
        self._failure_count += 1
        if self._state == CircuitState.HALF_OPEN or self._failure_count >= self.failure_threshold:
            if self._state != CircuitState.OPEN:
                logger.warning(
                    "circuit_breaker_opened",
                    breaker=self.name,
                    failure_count=self._failure_count,
                    recovery_timeout=self.recovery_timeout,
                )
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
