"""A loop stall must not extend a Git subprocess's physical lifetime."""

from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import psutil
import pytest

from tests.e2e import conftest
from tests.e2e.daemon_git_deadline_bootstrap import (
    BLOCKED,
    COMMAND,
    KILL_FILE,
    PID_FILE,
    RESULT,
    TIMEOUT_SECONDS,
)

pytestmark = pytest.mark.e2e

DEADLINE_ALLOWANCE_SECONDS = 1.5


@pytest.mark.parametrize("command", [b"r", b"s"], ids=["run", "stream"])
def test_git_deadline_survives_isolated_daemon_loop_stall(
    e2e_project_dir: Path,
    e2e_config: tuple[Path, int, int],
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    command: bytes,
) -> None:
    home = e2e_config[0].parent
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    executable = home / "git"
    executable.write_text('#!/bin/sh\nprintf "%s" "$$" > "$GIT_TEST_PID"\nexec /bin/sleep 60\n')
    executable.chmod(0o700)
    pid: int | None = None
    instance: conftest.DaemonInstance | None = None
    with contextlib.ExitStack() as channels:
        command_path = home / COMMAND
        blocked_path = home / BLOCKED
        result_path = home / RESULT
        for path in (command_path, blocked_path, result_path):
            os.mkfifo(path, mode=0o600)
        command_channel = channels.enter_context(
            os.fdopen(os.open(command_path, os.O_RDWR | os.O_NONBLOCK), "r+b", buffering=0)
        )
        blocked_channel = channels.enter_context(
            os.fdopen(os.open(blocked_path, os.O_RDWR | os.O_NONBLOCK), "r+b", buffering=0)
        )
        result_channel = channels.enter_context(
            os.fdopen(os.open(result_path, os.O_RDWR | os.O_NONBLOCK), "r+b", buffering=0)
        )
        daemon = conftest.spawn_daemon_instance(
            e2e_project_dir,
            e2e_config,
            runner_module="tests.e2e.daemon_git_deadline_bootstrap",
        )
        try:
            instance = next(daemon)
            assert instance.is_alive()
            command_channel.write(command)
            ready, _, _ = select.select([blocked_channel], [], [], 10.0)
            assert ready, "Isolated runner did not reach the deliberate loop stall"
            payload = blocked_channel.read(1024)
            assert payload is not None
            pid_text, start_text = payload.decode().split()
            pid = int(pid_text)
            started_at = float(start_text)
            # The 2.5s bound stays below the 4s loop stall and 60s natural exit.
            deadline = started_at + TIMEOUT_SECONDS + DEADLINE_ALLOWANCE_SECONDS
            try:
                with contextlib.suppress(psutil.NoSuchProcess):
                    psutil.Process(pid).wait(timeout=max(0.0, deadline - time.monotonic()))
            except psutil.TimeoutExpired:
                observed_at = time.monotonic()
                terminated_on_time = False
            else:
                observed_at = time.monotonic()
                terminated_on_time = observed_at <= deadline
            print(
                f"Git deadline observation: elapsed_seconds={observed_at - started_at:.6f} "
                f"bound_seconds={deadline - started_at:.6f} "
                f"overrun_seconds={observed_at - deadline:.6f} "
                f"terminated_on_time={terminated_on_time}"
            )
            record_property("deadline_elapsed_seconds", observed_at - started_at)
            record_property("deadline_bound_seconds", deadline - started_at)
            record_property("deadline_overrun_seconds", observed_at - deadline)
            ready, _, _ = select.select([result_channel], [], [], 5.0)
            assert ready, "Isolated runner never settled the Git result"
            assert result_channel.read(1024) == b"timeout"
            signalled_at = float((home / KILL_FILE).read_text())
            print(f"Git daemon kill signal: elapsed_seconds={signalled_at - started_at:.6f}")
            record_property("kill_signal_elapsed_seconds", signalled_at - started_at)
            assert signalled_at <= deadline, "Daemon signalled Git after the bounded deadline"
            assert terminated_on_time, (
                "Git subprocess survived its deadline while the isolated daemon loop was blocked"
            )
            assert not psutil.pid_exists(pid), "Git leader was not reaped"
        finally:
            # A failed reproduction must not leave its independent process group behind.
            pid_path = home / PID_FILE
            if pid is None and pid_path.exists():
                pid = int(pid_path.read_text())
            if pid is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
            if instance is not None and instance.is_alive():
                # Release the database-wide daemon lease before killing children.
                instance.process.send_signal(signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    instance.process.wait(timeout=15.0)
            daemon.close()
