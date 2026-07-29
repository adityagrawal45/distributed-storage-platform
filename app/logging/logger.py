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
