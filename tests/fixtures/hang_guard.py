"""Fail fast when a finished pytest run never exits.

pytest-timeout bounds each test, including fixture teardown. Nothing bounds the
exit after the last test, where a leaked non-daemon thread blocks interpreter
shutdown at 0% CPU. The stdlib watchdog armed here dumps every thread's stack
and exits the process instead.
"""

from __future__ import annotations

import faulthandler
import gc

import pytest

_STDERR_FD = 2


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        "exit_watchdog_timeout",
        "Seconds a finished run may take to exit before it dumps every thread's stack",
        default="60",
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session: pytest.Session) -> None:
    # Pytest's unconfigure forces five full collections, and interpreter shutdown adds
    # more. Each scans every tracked object, and the import graph alone is ~370k of them,
    # so under load the exit overran the watchdog below (#25804). Freezing the
    # import-time heap leaves those passes scanning only what the tests create.
    gc.freeze()


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    # The process's own stderr descriptor: pytest's capture has ended by now.
    faulthandler.dump_traceback_later(
        float(config.getini("exit_watchdog_timeout")), exit=True, file=_STDERR_FD
    )
