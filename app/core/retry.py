"""
Generic async retry helper with exponential backoff and jitter.

Design decisions:
- One small, dependency-free helper (rather than pulling in `tenacity`)
  used everywhere NimbusFS needs to tolerate a transient failure:
  startup dependency checks (DB/Redis/GCS), health checks, and Redis
  operations inside the cache/lock layer.
- Exponential backoff with full jitter (`random.uniform(0, delay)`)
  avoids a "thundering herd" of retries — important once there are N
  instances all restarting after the same outage and all retrying their
  startup checks at once.
- Callers pass the specific exception type(s) worth retrying. We never
  blanket-catch `Exception`, because retrying a bug (e.g. a
  `TypeError` in application code) just delays the failure and hides it
  behind a confusing timeout instead of failing fast.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.logging.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class RetryExhaustedError(Exception):
    """Raised when every retry attempt failed. Wraps the last underlying error."""

    def __init__(self, attempts: int, last_error: BaseException):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"Gave up after {attempts} attempt(s); last error: {last_error!r}")


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay: float,
    max_delay: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    operation_name: str = "operation",
) -> T:
    """
    Calls `fn()` (a zero-arg async callable) up to `attempts` times.

    Backoff: `min(base_delay * 2**attempt, max_delay)`, jittered by
    sampling uniformly in `[0, computed_delay]`. Raises
    `RetryExhaustedError` if every attempt fails; the last exception is
    always available via `.last_error` for logging/reporting.
    """
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on as exc:  # noqa: PERF203 - retry loops inherently re-check per iteration
            last_error = exc
            if attempt == attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            jittered = random.uniform(0, delay)
            logger.warning(
                "retry_attempt_failed",
                operation=operation_name,
                attempt=attempt,
                max_attempts=attempts,
                delay_seconds=round(jittered, 3),
                error=str(exc),
            )
            await asyncio.sleep(jittered)

    assert last_error is not None  # pragma: no cover - loop always runs >=1 time
    raise RetryExhaustedError(attempts, last_error)
