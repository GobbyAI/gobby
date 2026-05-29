"""Uvicorn shutdown log handling."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

_GRACEFUL_TIMEOUT_MESSAGE = "timeout graceful shutdown exceeded"
_RUNNING_TASKS_MESSAGE = "running task(s)"


class UvicornShutdownLogFilter(logging.Filter):
    """Downgrade expected uvicorn request cancellation during daemon shutdown."""

    def __init__(self, shutdown_in_progress: Callable[[], bool]) -> None:
        super().__init__()
        self._shutdown_in_progress = shutdown_in_progress

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._is_shutdown_in_progress():
            return True

        message = record.getMessage()
        if _is_graceful_task_timeout_message(message):
            _rewrite_as_info(
                record,
                "HTTP request tasks cancelled during daemon shutdown: " + message,
            )
            return True

        exc = _record_exception(record)
        if exc is not None and _is_expected_shutdown_cancellation(exc):
            _rewrite_as_info(
                record,
                "HTTP request task cancelled during daemon shutdown after graceful timeout",
            )
            record.exc_info = None
            record.exc_text = None
            return True

        return True

    def _is_shutdown_in_progress(self) -> bool:
        try:
            return bool(self._shutdown_in_progress())
        except Exception:
            return False


def install_uvicorn_shutdown_filter(
    shutdown_in_progress: Callable[[], bool],
) -> UvicornShutdownLogFilter:
    """Install a filter on uvicorn's error logger and return it for removal."""
    log_filter = UvicornShutdownLogFilter(shutdown_in_progress)
    logging.getLogger("uvicorn.error").addFilter(log_filter)
    return log_filter


def remove_uvicorn_shutdown_filter(log_filter: UvicornShutdownLogFilter) -> None:
    """Remove a previously installed uvicorn shutdown filter."""
    logging.getLogger("uvicorn.error").removeFilter(log_filter)


def _is_graceful_task_timeout_message(message: str) -> bool:
    return _RUNNING_TASKS_MESSAGE in message and _GRACEFUL_TIMEOUT_MESSAGE in message


def _record_exception(record: logging.LogRecord) -> BaseException | None:
    if not record.exc_info:
        return None
    if isinstance(record.exc_info, tuple):
        exc = record.exc_info[1]
        return exc if isinstance(exc, BaseException) else None
    return record.exc_info if isinstance(record.exc_info, BaseException) else None


def _is_expected_shutdown_cancellation(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.CancelledError):
        return _GRACEFUL_TIMEOUT_MESSAGE in str(exc)
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(
            _is_expected_shutdown_cancellation(child) for child in exc.exceptions
        )
    return False


def _rewrite_as_info(record: logging.LogRecord, message: str) -> None:
    record.levelno = logging.INFO
    record.levelname = "INFO"
    record.msg = message
    record.args = ()
