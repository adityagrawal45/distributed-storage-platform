"""
Structured JSON logging configuration using `structlog`.

Design decisions:
- JSON output (in non-dev environments) is required so logs are directly
  ingestible by Google Cloud Logging / any log aggregator without a
  separate parsing step.
- In development, a human-readable colored console renderer is used
  instead, for developer ergonomics.
- `structlog` is configured to merge in stdlib `logging` records too, so
  third-party libraries (uvicorn, sqlalchemy) produce consistently
  formatted output alongside our own structured events.
- `get_logger(__name__)` mirrors stdlib usage so it's a drop-in mental
  model for engineers used to `logging.getLogger(__name__)`.
"""

import logging
import sys

import structlog

from app.core.config import get_settings

settings = get_settings()

#: Phase 11 security audit (see docs/observability.md "Security Audit
#: of Logging"): NimbusFS's own log call sites were already found to be
#: careful — nowhere does the codebase pass a raw password, JWT, or
#: signed URL to `logger.*()`. This processor is defense-in-depth on top
#: of that, not evidence a leak was found: a future call site, or a
#: third-party library's log record merged in by `merge_contextvars`,
#: could still bind a field under one of these names. Matching is on the
#: KEY, not a regex over values, which is O(1) per field and cannot
#: accidentally redact legitimate content that merely looks secret-shaped.
_REDACTED_KEYS = frozenset(
    {
        "password",
        "hashed_password",
        "access_token",
        "refresh_token",
        "token",
        "authorization",
        "jwt",
        "secret",
        "client_secret",
        "api_key",
        "private_key",
        "signed_url",
        "database_url",
        "redis_url",
    }
)
_REDACTED_VALUE = "***REDACTED***"


def _redact_sensitive_fields(_logger, _method_name, event_dict: dict) -> dict:
    """A structlog processor: replaces any event-dict key in `_REDACTED_KEYS`
    (case-insensitive) with a fixed placeholder, never the real value."""
    for key in list(event_dict.keys()):
        if key.lower() in _REDACTED_KEYS:
            event_dict[key] = _REDACTED_VALUE
    return event_dict


def configure_logging() -> None:
    """Configure stdlib logging + structlog. Call once at startup."""
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Phase 11: last, so it sees (and can redact) fields merged in by
        # every processor above it, including contextvars.
        _redact_sensitive_fields,
    ]

    if settings.LOG_JSON:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(log_level)

    # Tame noisy third-party loggers.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to the given module name."""
    return structlog.get_logger(name)
