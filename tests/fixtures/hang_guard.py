"""Fail fast when a finished pytest run never exits.

pytest-timeout bounds each test, including fixture teardown. Nothing bounds the
exit after the last test, where a leaked non-daemon thread blocks interpreter
shutdown at 0% CPU. The stdlib watchdog armed here dumps every thread's stack
and exits the process instead.
"""

from __future__ import annotations

import faulthandler
import gc
import sys

import pytest

_STDERR_FD = 2


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        "exit_watchdog_timeout",
        "Seconds a finished run may take to exit before it dumps every thread's stack",
        default="60",
    )


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    # Teardown runs several full collections (pytest's unraisable-exception cleanup).
    # The run's heap is still live, so each one re-traverses every object the suite built.
    # Freezing it keeps those passes proportional to what teardown creates, not to the
    # whole suite; under load the unfrozen passes alone outlasted the watchdog below.
    gc.freeze()
    # The watchdog exits without flushing stdio, so flush the run summary first.
    sys.stdout.flush()
    # The process's own stderr descriptor: pytest's capture has ended by now.
    faulthandler.dump_traceback_later(
        float(config.getini("exit_watchdog_timeout")), exit=True, file=_STDERR_FD
    )
