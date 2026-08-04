"""
In-process circuit breaker.

Design decisions:
- This guards calls to a dependency that can degrade gracefully if it's
  down (Redis, in this phase). It deliberately does NOT guard PostgreSQL
  or GCS — those are hard dependencies the app cannot function without,
  so they fail fast via `app.core.retry` at startup/request time instead
  of degrading.
- State (`CLOSED`/`OPEN`/`HALF_OPEN`, failure count, last-failure time)
  lives in process memory, per instance. This is NOT a violation of the
  "no local memory for application state" rule in README "Stateless
  Design": circuit breaker state describes *this process's local
  judgment about a dependency's health*, not any user- or file-visible
  data. Two instances are allowed to disagree about whether Redis looks
  healthy right now — each just makes its own best-effort call, and both
  converge once Redis actually recovers. If this state lived in Redis
  itself, a Redis outage would corrupt the very breaker meant to protect
  callers from that outage.
- Classic three-state machine:
    CLOSED     - calls pass through normally; failures increment a counter.
    OPEN       - calls are short-circuited immediately (no network call at
                 all) until `reset_timeout_seconds` has elapsed.
    HALF_OPEN  - after the timeout, exactly one trial call is allowed
                 through; success -> CLOSED, failure -> OPEN again.
"""

import time
from enum import Enum

from app.logging.logger import get_logger

logger = get_logger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised by `CircuitBreaker.guard()` when the circuit is open (call skipped entirely)."""


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int, reset_timeout_seconds: float):
        self.name = name
        self._failure_threshold = failure_threshold
        self._reset_timeout_seconds = reset_timeout_seconds
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            if (time.monotonic() - self._opened_at) >= self._reset_timeout_seconds:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def guard(self) -> None:
        """Raises `CircuitOpenError` if a call should be skipped right now."""
        if self.state == CircuitState.OPEN:
            raise CircuitOpenError(f"Circuit '{self.name}' is open; skipping call.")

    def record_success(self) -> None:
        if self._state != CircuitState.CLOSED:
            logger.info("circuit_breaker_closed", circuit=self.name)
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failure_count += 1
        if self.state == CircuitState.HALF_OPEN or self._failure_count >= self._failure_threshold:
            if self._state != CircuitState.OPEN:
                logger.warning(
                    "circuit_breaker_opened",
                    circuit=self.name,
                    failure_count=self._failure_count,
                    reset_timeout_seconds=self._reset_timeout_seconds,
                )
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
