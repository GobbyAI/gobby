"""Fail fast when a finished pytest run never exits.

pytest-timeout bounds each test, including fixture teardown. Nothing bounds the
exit after the last test, where a leaked non-daemon thread blocks interpreter
shutdown at 0% CPU. The stdlib watchdog armed here dumps every thread's stack
and exits the process instead. The exit has three phases with separate bounds: pytest's
cleanup stack, the thread join, and atexit. Only the thread join gets the short bound;
the other two are bounded work that can run slow under load.
"""

from __future__ import annotations

import atexit
import faulthandler
import functools
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
    parser.addini(
        "teardown_watchdog_timeout",
        "Seconds pytest's cleanup stack and the atexit phase may take before the dump",
        default="600",
    )


def _arm_exit_watchdog(seconds: float) -> None:
    # Flush first: faulthandler's exit=True skips stdio flushing, so output still
    # buffered when the watchdog fires is lost. Arming again replaces the previous timer.
    sys.stdout.flush()
    sys.stderr.flush()
    # The process's own stderr descriptor: pytest's capture has ended by now.
    faulthandler.dump_traceback_later(seconds, exit=True, file=_STDERR_FD)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    # Registered before the other configure-time cleanups, so it runs after them: the
    # thread-join bound starts only once pytest's cleanup stack, including the gc passes
    # of the unraisable-exception cleanup, has finished.
    config.add_cleanup(
        functools.partial(_arm_exit_watchdog, float(config.getini("exit_watchdog_timeout")))
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
    # Interpreter finalization collects every live object, and a session heap of about
    # 378k objects took 2 to 17 s per full pass under load: that overran the 60 s guard
    # on a clean run. Freezing first moves those objects to the permanent generation so
    # the exit passes skip them; the guards then bound only the exit work that remains.
    gc.freeze()
    # Bounds pytest's cleanup stack, which runs after this hook.
    teardown_timeout = float(config.getini("teardown_watchdog_timeout"))
    _arm_exit_watchdog(teardown_timeout)
    # Registered last, so it runs first among atexit callbacks: the thread-join bound
    # ends here and the teardown bound takes over. Re-arming rather than cancelling keeps
    # a stuck atexit callback, such as a multiprocessing exit join, bounded.
    atexit.register(_arm_exit_watchdog, teardown_timeout)
