"""Structured logging: JSON on standard output, with secrets and personal data kept out.

What the log must never hold (spec section 10.2):
- secrets: a processor removes keys such as `password` and `token`;
- local variables: the exception renderer runs with `show_locals=False`;
- database error text: PostgreSQL echoes row values such as `Key (email)=(a@x.in)`, so a database
  error is reduced to its class, SQLSTATE and constraint name;
- the query string: Uvicorn's access log prints it, so that logger is disabled.
"""

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog
from sqlalchemy.exc import DBAPIError
from structlog.tracebacks import ExceptionDictTransformer
from structlog.typing import EventDict, WrappedLogger

from app.core.config import Settings

SENSITIVE_KEY = re.compile(r"password|token|secret|authorization|signature", re.IGNORECASE)


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Remove every key that looks like a secret, at any depth."""

    def clean(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {k: clean(v) for k, v in value.items() if not SENSITIVE_KEY.search(str(k))}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    for key in [key for key in event_dict if SENSITIVE_KEY.search(key)]:
        del event_dict[key]
    for key, value in event_dict.items():
        event_dict[key] = clean(value)
    return event_dict


def _exception_of(exc_info: object) -> BaseException | None:
    """The exception behind structlog's `exc_info`: True, an exception or an exc_info tuple."""
    if exc_info is True:
        return sys.exc_info()[1]
    if isinstance(exc_info, tuple) and len(exc_info) == 3:
        exc_info = exc_info[1]
    return exc_info if isinstance(exc_info, BaseException) else None


def scrub_database_errors(
    _logger: WrappedLogger, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Reduce a database error to its class, SQLSTATE and constraint name.

    It walks `__cause__` and `__context__`, so a database error wrapped in another exception is
    scrubbed too (sharp edge 33). The traceback goes with it, because the traceback text carries
    the error message.
    """
    current = _exception_of(event_dict.get("exc_info"))
    while current is not None:
        if isinstance(current, DBAPIError):
            diag = getattr(current.orig, "diag", None)
            event_dict.pop("exc_info", None)
            event_dict["error"] = {
                "type": type(current.orig).__name__,
                "sqlstate": getattr(current.orig, "sqlstate", None),
                "constraint": getattr(diag, "constraint_name", None),
            }
            break
        current = current.__cause__ or current.__context__
    return event_dict


class DropTraceback(logging.Filter):
    """Remove the traceback from a record. `uvicorn.error` would print the database DETAIL line."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.exc_info = None
        record.exc_text = None
        return True


def protect_uvicorn_loggers() -> None:
    """Silence the access log (it prints query strings) and drop tracebacks from `uvicorn.error`.

    This must run in the lifespan start-up. Uvicorn applies its own logging configuration after
    the application module is imported, so a call at import time is too early (spec check X).
    """
    logging.getLogger("uvicorn.access").disabled = True
    error_logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, DropTraceback) for f in error_logger.filters):
        error_logger.addFilter(DropTraceback())


def configure_logging(settings: Settings) -> None:
    processors: list[Any] = [
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,
        scrub_database_errors,
    ]
    if settings.log_format == "json":
        # The default renderer prints local variables, a password among them (spec check A).
        processors.append(
            structlog.processors.ExceptionRenderer(ExceptionDictTransformer(show_locals=False))
        )
        processors.append(structlog.processors.JSONRenderer())  # also escapes line breaks
    else:
        processors.append(
            structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback)
        )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,  # tests reconfigure structlog
    )
    protect_uvicorn_loggers()
