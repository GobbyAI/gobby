"""ProbeBatch runs independent probes concurrently under one shared deadline."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from gobby.utils.probes import REAP_GRACE_SECONDS, ProbeBatch

pytestmark = pytest.mark.unit


@pytest.fixture
def release() -> Iterator[threading.Event]:
    gate = threading.Event()
    yield gate
    gate.set()


def test_collect_returns_at_the_deadline_and_names_the_slow_probes(
    release: threading.Event,
) -> None:
    started = time.monotonic()
    batch = ProbeBatch(
        {
            "fast": lambda: "ready",
            "slow_a": lambda: release.wait(10),
            "slow_b": lambda: release.wait(10),
        },
        deadline=started + 0.2,
    )

    results = batch.collect()

    assert time.monotonic() - started < 1.5
    assert results.values == {"fast": "ready"}
    assert results.timed_out == ("slow_a", "slow_b")


def test_probes_run_concurrently_without_a_deadline() -> None:
    # The barrier only opens when all five probes are running at the same time.
    barrier = threading.Barrier(5, timeout=5)
    batch = ProbeBatch(
        {f"probe_{index}": barrier.wait for index in range(5)},
        deadline=None,
    )

    results = batch.collect()

    assert sorted(results.values) == [f"probe_{index}" for index in range(5)]
    assert sorted(results.values.values()) == [0, 1, 2, 3, 4]
    assert results.timed_out == ()


DEADLINE_SECONDS = 0.2
# Headroom for the separate interpreter to tear down after collect returns.
EXIT_TOLERANCE_SECONDS = 1.0


def _run_batch_in_fresh_interpreter(probe_source: str) -> tuple[float, str]:
    """Run one hung probe under a deadline in a new interpreter.

    Returns the seconds from the batch start to that interpreter's exit, and the
    printed timed_out tuple. time.monotonic() is one system-wide clock on macOS
    and Linux, so the child's start stamp is comparable here.
    """
    script = (
        "import threading, time\n"
        "from gobby.utils import spawn\n"
        "from gobby.utils.probes import ProbeBatch\n"
        f"def probe():\n    {probe_source}\n"
        "started = time.monotonic()\n"
        "print(started, flush=True)\n"
        f"results = ProbeBatch({{'hung': probe}}, deadline=started + {DEADLINE_SECONDS})"
        ".collect()\n"
        "print(results.timed_out)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=False
    )
    exited = time.monotonic()
    assert completed.returncode == 0, completed.stderr
    started_line, timed_out_line = completed.stdout.splitlines()
    return exited - float(started_line), timed_out_line


def test_a_hung_subprocess_probe_is_killed_and_reaped_by_the_deadline(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    probe = (
        "spawn.run(['/bin/sh', '-c', 'echo $$ > \"$0\"; exec /bin/sleep 30', "
        f"{str(pid_file)!r}], timeout=10)"
    )

    elapsed, timed_out = _run_batch_in_fresh_interpreter(probe)

    child_pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        child_alive = False
    else:
        child_alive = True
        os.kill(child_pid, signal.SIGKILL)
    assert not child_alive, "probe subprocess outlived the process that started it"
    assert timed_out == "('hung',)"
    assert elapsed < DEADLINE_SECONDS + REAP_GRACE_SECONDS + EXIT_TOLERANCE_SECONDS


def test_a_hung_pure_python_probe_does_not_hold_the_interpreter_open_at_exit() -> None:
    elapsed, timed_out = _run_batch_in_fresh_interpreter("threading.Event().wait(30)")

    assert timed_out == "('hung',)"
    assert elapsed < DEADLINE_SECONDS + REAP_GRACE_SECONDS + EXIT_TOLERANCE_SECONDS


def test_a_failing_probe_propagates_its_exception() -> None:
    def broken() -> str:
        raise RuntimeError("probe exploded")

    batch = ProbeBatch({"broken": broken}, deadline=None)

    with pytest.raises(RuntimeError, match="probe exploded"):
        batch.collect()
