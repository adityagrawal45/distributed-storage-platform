"""
Lightweight distributed tracing (Phase 11).

Why a hand-rolled span primitive instead of the OpenTelemetry SDK
-------------------------------------------------------------------
The Phase 11 brief explicitly warns against adding OpenTelemetry (or any
other new infrastructure component) "simply because it is commonly
used", and requires a clear architectural justification for anything
beyond what already exists. NimbusFS already has exactly what real
distributed tracing needs to be useful for this system's actual size:

  * a `trace_id` generated/propagated per HTTP request
    (`app/middleware/request_context.py`, Phase 4),
  * a `correlation_id`/`causation_id` chain propagated through the
    transactional outbox -> Pub/Sub -> worker path
    (`app/events/envelope.py`, `app/events/emitter.py`, Phase 8),
  * structured JSON logs already ingested by Cloud Logging, which
    natively promotes a `trace`/`spanId`-shaped field into Cloud Trace's
    log-correlation view without any exporter.

What was missing was: (a) `trace_id` was not threaded across the
Pub/Sub hop (a worker started a *new* implicit trace context per
message with no link back to the HTTP request that caused it), and
(b) there was no notion of a *span* — a named, timed sub-operation
within a request/message, with parent/child nesting, distinguishing
"the request took 800ms" from "800ms of which 650ms was one GCS call".

This module adds both, as structured log events (`span_started` /
`span_completed` / `span_failed`) carrying `trace_id`, `span_id`,
`parent_span_id`, `operation`, and `duration_ms` — sufficient to
reconstruct a request's timeline from Cloud Logging (or any log
aggregator) by querying on `trace_id`, exactly the workflow
`docs/observability.md`'s "Incident Investigation" section walks
through. It does NOT export to Cloud Trace's span API, is not
OpenTelemetry-wire-compatible, and does not do sampling decisions at
the collector level — seeing this as a real gap (not a hidden one) is
recorded honestly in `docs/observability.md`'s "Remaining Risks"
section: if/when per-span latency percentiles or a trace waterfall UI
are needed, migrating these call sites to real OpenTelemetry spans is a
mechanical, incremental change (this module's call sites already mark
exactly where a span should start/stop) — it is not blocked by
anything built here.

Design
------
- `start_span("operation", **fields)` is a synchronous context manager
  (spans wrap `await` expressions fine — nothing here needs to be async
  itself, it only mutates contextvars and logs before/after).
- Nesting: a span started while another is active becomes its child
  (`parent_span_id` = the enclosing span's id), so a request's spans
  form a tree, exactly like a real trace.
- Best-effort: a logging failure inside a span never propagates as a
  new exception distinct from whatever the wrapped code itself raised
  (Phase 11 principle: telemetry must never become a failure amplifier).
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import structlog

from app.logging.logger import get_logger

logger = get_logger(__name__)


def new_span_id() -> str:
    """A short, log-friendly span identifier (not a UUID's full length)."""
    return uuid.uuid4().hex[:16]


def current_trace_id() -> str | None:
    """The trace ID bound for the current request/message, if any."""
    return structlog.contextvars.get_contextvars().get("trace_id")


def current_span_id() -> str | None:
    return structlog.contextvars.get_contextvars().get("span_id")


@contextmanager
def start_span(operation: str, **fields: Any) -> Iterator[str]:
    """
    Times `operation`, binding `span_id`/`parent_span_id`/`operation`
    into structlog's contextvars for the duration of the `with` block so
    every log line emitted inside it (including from nested spans)
    carries them automatically — the same zero-boilerplate propagation
    `RequestContextMiddleware` already relies on for `trace_id`.

    `**fields` are extra structured fields logged on start/completion
    only (e.g. `bucket=...`, `object_name=...`) — kept OUT of the
    contextvars binding so they don't leak into unrelated log lines
    emitted deeper in the call stack.
    """
    parent_span_id = current_span_id()
    span_id = new_span_id()
    tokens = structlog.contextvars.bind_contextvars(
        span_id=span_id,
        parent_span_id=parent_span_id,
    )
    started = time.perf_counter()
    try:
        logger.debug("span_started", operation=operation, **fields)
    except Exception:  # noqa: BLE001 - telemetry must never break the caller
        pass

    try:
        yield span_id
    except Exception as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            logger.warning(
                "span_failed",
                operation=operation,
                duration_ms=duration_ms,
                error_type=type(exc).__name__,
                error=str(exc),
                **fields,
            )
        except Exception:  # noqa: BLE001
            pass
        raise
    else:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            logger.info("span_completed", operation=operation, duration_ms=duration_ms, **fields)
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            structlog.contextvars.reset_contextvars(**tokens)
        except Exception:  # noqa: BLE001
            pass
