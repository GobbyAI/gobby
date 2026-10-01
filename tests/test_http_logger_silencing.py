"""Third-party HTTP client loggers stay off INFO in the daemon and CLI (#22866)."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from gobby.cli.utils_process import setup_logging
from gobby.utils.logging import HTTP_CLIENT_LOGGERS, silence_http_client_loggers

pytestmark = pytest.mark.unit


@pytest.fixture
def reset_http_logger_levels() -> Iterator[None]:
    loggers = [logging.getLogger(name) for name in HTTP_CLIENT_LOGGERS]
    saved = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.NOTSET)
    yield
    for logger, level in zip(loggers, saved, strict=True):
        logger.setLevel(level)


def _levels() -> list[int]:
    return [logging.getLogger(name).level for name in HTTP_CLIENT_LOGGERS]


def test_http_client_loggers_include_httpx2() -> None:
    assert set(HTTP_CLIENT_LOGGERS) == {"httpx", "httpx2", "httpcore"}


@pytest.mark.usefixtures("reset_http_logger_levels")
def test_silence_http_client_loggers_sets_warning() -> None:
    silence_http_client_loggers()

    assert _levels() == [logging.WARNING] * len(HTTP_CLIENT_LOGGERS)


@pytest.mark.usefixtures("reset_http_logger_levels")
def test_cli_setup_logging_silences_http_client_info() -> None:
    setup_logging(verbose=True)

    assert _levels() == [logging.WARNING] * len(HTTP_CLIENT_LOGGERS)
