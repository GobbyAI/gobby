"""A hung test run fails fast with every thread's stack (#25645).

Each case runs an inner pytest in its own rootdir: a timeout exits that whole
process, and the inner run must not load this suite's conftest or database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]

_TEST_HANG = """
    import threading

    def _blocked_test_body() -> None:
        threading.Event().wait()

    def test_hangs() -> None:
        _blocked_test_body()
"""

_EXIT_HANG = """
    import threading

    _release = threading.Event()

    def _leaked_worker() -> None:
        _release.wait()

    def test_leaves_a_thread_running() -> None:
        threading.Thread(target=_leaked_worker).start()
"""

_SLOW_ATEXIT = """
    import atexit
    import time

    def test_passes() -> None:
        atexit.register(time.sleep, 3)
"""

_ATEXIT_HANG = """
    import atexit
    import threading

    def _blocked_atexit() -> None:
        threading.Event().wait()

    def test_passes() -> None:
        atexit.register(_blocked_atexit)
"""

_PASSING = """
    def test_passes() -> None:
        pass
"""


def _run_inner(
    pytestconfig: pytest.Config, tmp_path: Path, body: str, *overrides: str
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "test_inner.py").write_text(textwrap.dedent(body))
    command = [
        sys.executable,
        "-m",
        "pytest",
        *pytestconfig.getini("addopts"),
        "-p",
        "no:cacheprovider",
        "-p",
        "tests.fixtures.hang_guard",
        "--rootdir",
        str(tmp_path),
        "-o",
        f"timeout_method={pytestconfig.getini('timeout_method')}",
        *overrides,
        "test_inner.py",
    ]
    environment = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}
    return subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_project_bounds_each_test_with_a_stack_dump(pytestconfig: pytest.Config) -> None:
    assert float(pytestconfig.getini("timeout")) == 300
    assert pytestconfig.getini("timeout_method") == "thread"


def test_hung_test_exits_with_its_stack(pytestconfig: pytest.Config, tmp_path: Path) -> None:
    result = _run_inner(pytestconfig, tmp_path, _TEST_HANG, "-o", "timeout=1")

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Timeout" in output
    assert "_blocked_test_body" in output


def test_run_that_never_exits_dumps_its_threads(
    pytestconfig: pytest.Config, tmp_path: Path
) -> None:
    result = _run_inner(pytestconfig, tmp_path, _EXIT_HANG, "-o", "exit_watchdog_timeout=1")

    output = result.stdout + result.stderr
    assert "1 passed" in output
    assert result.returncode != 0
    assert "_leaked_worker" in output


def test_slow_atexit_cleanup_after_threads_join_is_not_a_hang(
    pytestconfig: pytest.Config, tmp_path: Path
) -> None:
    result = _run_inner(pytestconfig, tmp_path, _SLOW_ATEXIT, "-o", "exit_watchdog_timeout=1")

    output = result.stdout + result.stderr
    assert "1 passed" in output
    assert result.returncode == 0
    assert "Timeout" not in output


def test_atexit_callback_that_never_returns_dumps_and_exits(
    pytestconfig: pytest.Config, tmp_path: Path
) -> None:
    # The teardown bound also covers pytest's cleanup stack, which runs before atexit and
    # can take over a second under load. The bound must clear it, so the dump is the
    # atexit hang: the _blocked_atexit assertion fails if the dump landed in cleanup.
    result = _run_inner(
        pytestconfig,
        tmp_path,
        _ATEXIT_HANG,
        "-o",
        "exit_watchdog_timeout=5",
        "-o",
        "teardown_watchdog_timeout=10",
    )

    output = result.stdout + result.stderr
    assert "1 passed" in output
    assert result.returncode != 0
    assert "Timeout (0:00:10)!" in output
    assert "_blocked_atexit" in output


def test_stdio_is_flushed_before_the_watchdog_arms(tmp_path: Path) -> None:
    # faulthandler's exit skips stdio flushing. A write still buffered when the watchdog
    # fires would be lost, so arming flushes first. The probe's stdout is a block-buffered
    # pipe, as it is under pytest's runners.
    probe = textwrap.dedent(
        """
        import sys
        import time

        from tests.fixtures.hang_guard import _arm_exit_watchdog

        sys.stdout.write("written before the watchdog arms\\n")
        _arm_exit_watchdog(1)
        time.sleep(30)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode != 0
    assert "written before the watchdog arms" in result.stdout


def test_slow_cleanup_stack_after_the_pass_summary_is_not_a_hang(
    pytestconfig: pytest.Config, tmp_path: Path
) -> None:
    # pytest's cleanup stack runs after the pass summary and before the thread join. Slow
    # cleanup there is bounded work, so it must not trip the 1 s thread-join bound.
    (tmp_path / "slow_cleanup.py").write_text(
        "import time\n\n"
        "def pytest_configure(config):\n"
        "    config.add_cleanup(_slow_cleanup)\n\n"
        "def _slow_cleanup():\n"
        "    time.sleep(3)\n"
    )
    result = _run_inner(
        pytestconfig,
        tmp_path,
        _PASSING,
        "-o",
        "exit_watchdog_timeout=1",
        "-p",
        "slow_cleanup",
    )

    output = result.stdout + result.stderr
    assert "1 passed" in output
    assert result.returncode == 0
    assert "Timeout" not in output
