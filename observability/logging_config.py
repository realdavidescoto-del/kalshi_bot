import logging
import sys
import threading
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from pythonjsonlogger import jsonlogger

correlation_id_var: ContextVar[str | None] = ContextVar(
    "correlation_id", default=None
)
module_levels: dict[str, int] = {}


class CorrelationIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        cid = correlation_id_var.get()
        if cid:
            record.correlation_id = cid
        else:
            record.correlation_id = "-"
        return True


_SENSITIVE_KEYS = frozenset({
    "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "FRED_API_KEY",
    "KALSHI_ACCESS_KEY", "KALSHI_ACCESS_SECRET", "ALPHA_VANTAGE_API_KEY",
})


class StructuredJsonFormatter(jsonlogger.JsonFormatter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ):
        super().add_fields(log_record, record, message_dict)
        log_record["timestamp"] = datetime.now(UTC).isoformat(
            timespec="milliseconds"
        )
        log_record["level"] = record.levelname
        log_record["logger"] = record.name
        log_record["correlation_id"] = getattr(record, "correlation_id", "-")
        log_record["thread_id"] = threading.get_ident()
        for key in _SENSITIVE_KEYS:
            lc_key = key.lower()
            if lc_key in log_record:
                val = str(log_record[lc_key])
                if val and len(val) > 4:
                    log_record[lc_key] = val[:2] + "****" + val[-2:]
        if hasattr(record, "extra_fields"):
            log_record.update(record.extra_fields)


def setup_structured_logging(
    level: str = "INFO",
    log_file: str | None = None,
    module_levels_config: dict[str, str] | None = None,
):
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper()))

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(StructuredJsonFormatter())
    console_handler.addFilter(CorrelationIdFilter())
    root_logger.addHandler(console_handler)

    if log_file:
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5
        )
        file_handler.setFormatter(StructuredJsonFormatter())
        file_handler.addFilter(CorrelationIdFilter())
        root_logger.addHandler(file_handler)

    if module_levels_config:
        for module, mod_level in module_levels_config.items():
            logging.getLogger(module).setLevel(getattr(logging, mod_level.upper()))


def set_correlation_id(cid: str | None = None) -> str:
    if cid is None:
        cid = str(uuid.uuid4())[:8]
    correlation_id_var.set(cid)
    return cid


def get_correlation_id() -> str | None:
    return correlation_id_var.get()


def clear_correlation_id():
    correlation_id_var.set(None)


class LogContext:
    def __init__(self, **extra_fields):
        self.extra_fields = extra_fields
        self.old_cid = get_correlation_id()

    def __enter__(self):
        if "correlation_id" in self.extra_fields:
            set_correlation_id(self.extra_fields["correlation_id"])
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.old_cid:
            set_correlation_id(self.old_cid)
        else:
            clear_correlation_id()


def log_with_context(logger: logging.Logger, level: int, message: str, **extra_fields):
    extra = {"extra_fields": extra_fields}
    logger.log(level, message, extra=extra)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
