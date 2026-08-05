"""
Generic async retry helper with exponential backoff + jitter (Phase 4).

Design decisions:
- Implemented by hand rather than pulling in `tenacity`: the policy we
  need (bounded attempts, exponential backoff, full jitter, retry only
  on a caller-specified exception set) is ~30 lines, and one fewer
  dependency is one fewer thing to audit/pin/patch.
- `retry_async` is a plain async function, not a decorator, so call
  sites stay explicit about *what* is being retried and *why* — critical
  for startup code where "silently retrying forever" would hide a real
  outage. Callers pass a zero-arg async callable (typically a small
  lambda or closure).
- Full jitter (`random.uniform(0, delay)`) rather than plain exponential
  backoff: when many replicas start simultaneously (a rolling deploy or
  a crash-loop across a whole fleet) and all lose the DB/Redis at once,
  synchronized retries would create a thundering herd against the
  database exactly as it's recovering. Jitter spreads reconnect attempts
  out in time.
- Used for two very different situations with the same shape:
    1. Startup dependency checks (`app/main.py`) — bounded, fail-fast if
       exhausted, because a replica that can never reach its database
       should never accept traffic.
    2. Best-effort request-path operations where a transient blip
       shouldn't fail a whole request (e.g. one Redis idempotency-key
       lookup) — bounded, but the caller decides whether to degrade
       gracefully on exhaustion rather than raising.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.logging.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class RetryExhaustedError(Exception):
    """Raised when all retry attempts fail. Wraps the last underlying exception."""

    def __init__(self, attempts: int, last_exception: BaseException):
        self.attempts = attempts
        self.last_exception = last_exception
        super().__init__(f"Retry exhausted after {attempts} attempt(s): {last_exception!r}")


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    base_delay: float,
    max_delay: float,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    operation_name: str = "operation",
) -> T:
    """
    Retries `operation()` with exponential backoff + full jitter.

    Raises `RetryExhaustedError` (wrapping the last exception) if every
    attempt fails. Does NOT catch exceptions outside `retry_on` — those
    propagate immediately, unretried, on the assumption they're not
    transient (e.g. a validation error has no business being retried).
    """
    last_exception: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except retry_on as exc:  # noqa: PERF203 - retry loop, not a hot path
            last_exception = exc
            if attempt == max_attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            jittered_delay = random.uniform(0, delay)
            logger.warning(
                "retry_attempt_failed",
                operation=operation_name,
                attempt=attempt,
                max_attempts=max_attempts,
                delay_seconds=round(jittered_delay, 3),
                error=str(exc),
            )
            await asyncio.sleep(jittered_delay)

    assert last_exception is not None  # unreachable with max_attempts >= 1
    logger.error("retry_exhausted", operation=operation_name, attempts=max_attempts, error=str(last_exception))
    raise RetryExhaustedError(max_attempts, last_exception)
