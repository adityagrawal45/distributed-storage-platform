import logging
import sys
from logging.config import dictConfig
from app.core.config import settings


def setup_logging():
    log_level = settings.LOG_LEVEL.upper()
    if settings.LOG_FORMAT == "json":
        # Use python-json-logger if installed; fallback to basic
        try:
            from pythonjsonlogger import jsonlogger

            class CustomJsonFormatter(jsonlogger.JsonFormatter):
                def add_fields(self, log_record, record, message_dict):
                    super().add_fields(log_record, record, message_dict)
                    log_record["level"] = record.levelname
                    log_record["name"] = record.name

            formatter = CustomJsonFormatter()
        except ImportError:
            formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    else:
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(handler)

    # Suppress noisy logs from libraries
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)