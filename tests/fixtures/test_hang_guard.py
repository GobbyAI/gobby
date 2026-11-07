"""A hung test run fails fast with every thread's stack (#25645).

Each case runs an inner pytest in its own rootdir: a timeout exits that whole
process, and the inner run must not load this suite's conftest or database.
"""

from __future__ import annotations

import faulthandler
import gc
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.fixtures import hang_guard

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


@pytest.mark.unit
def test_exit_watchdog_freezes_the_session_heap_before_it_arms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Finalization garbage-collects every live object once the process exits, and
    # that work is what overran the watchdog. The freeze must come before the timer
    # arms so the exit passes skip the session's objects, not after.
    calls: list[str] = []
    monkeypatch.setattr(gc, "freeze", lambda: calls.append("freeze"))
    monkeypatch.setattr(faulthandler, "dump_traceback_later", lambda *_a, **_k: calls.append("arm"))
    config = MagicMock(spec=pytest.Config)
    config.getini.return_value = "60"

    hang_guard.pytest_unconfigure(config)

    assert calls == ["freeze", "arm"]
