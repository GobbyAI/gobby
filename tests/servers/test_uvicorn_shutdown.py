"""Uvicorn shutdown log filtering tests."""

import asyncio
import logging

import pytest

from gobby.servers.uvicorn_shutdown import UvicornShutdownLogFilter

pytestmark = pytest.mark.unit


def _record(
    message: str,
    *,
    args: tuple[object, ...] = (),
    exc: BaseException | None = None,
) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    return logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        __file__,
        1,
        message,
        args,
        exc_info,
    )


def test_shutdown_filter_downgrades_uvicorn_task_timeout() -> None:
    log_filter = UvicornShutdownLogFilter(lambda: True)
    record = _record(
        "Cancel %s running task(s), timeout graceful shutdown exceeded",
        args=(90,),
    )

    assert log_filter.filter(record) is True

    assert record.levelno == logging.INFO
    assert record.levelname == "INFO"
    assert "90 running task(s)" in record.getMessage()


def test_shutdown_filter_downgrades_expected_asgi_cancellation() -> None:
    log_filter = UvicornShutdownLogFilter(lambda: True)
    exc = asyncio.CancelledError("Task cancelled, timeout graceful shutdown exceeded")
    record = _record("Exception in ASGI application\n", exc=exc)

    assert log_filter.filter(record) is True

    assert record.levelno == logging.INFO
    assert record.levelname == "INFO"
    assert record.exc_info is None
    assert "cancelled during daemon shutdown" in record.getMessage()


def test_shutdown_filter_preserves_non_shutdown_errors() -> None:
    log_filter = UvicornShutdownLogFilter(lambda: True)
    exc = RuntimeError("boom")
    record = _record("Exception in ASGI application\n", exc=exc)

    assert log_filter.filter(record) is True

    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.getMessage() == "Exception in ASGI application\n"


def test_shutdown_filter_preserves_records_when_shutdown_is_not_active() -> None:
    log_filter = UvicornShutdownLogFilter(lambda: False)
    record = _record(
        "Cancel %s running task(s), timeout graceful shutdown exceeded",
        args=(90,),
    )

    assert log_filter.filter(record) is True

    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Cancel 90 running task(s), timeout graceful shutdown exceeded"
