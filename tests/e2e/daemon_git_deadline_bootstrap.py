"""Block only an isolated test runner's loop while real daemon_git is active."""

from __future__ import annotations

import asyncio
import os
import runpy
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import gobby.utils.daemon_git as daemon_git_module
from gobby import runner_lifecycle
from gobby.runner_pid_file import PidOwnershipResolution
from gobby.utils.daemon_git import DaemonGitService

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner
    from gobby.utils.daemon_git import _GitProcess

COMMAND = "git-deadline-command"
BLOCKED = "git-deadline-blocked"
RESULT = "git-deadline-result"
PID_FILE = "git-deadline-pid"
KILL_FILE = "git-deadline-kill-time"
TIMEOUT_SECONDS = 1.0
BLOCK_SECONDS = 4.0


def _read_command(home: Path) -> bytes:
    with (home / COMMAND).open("rb", buffering=0) as channel:
        return channel.read(1)


async def _reproduce(home: Path) -> None:
    command = await asyncio.to_thread(_read_command, home)
    env = {"PATH": str(home), "GIT_TEST_PID": str(home / PID_FILE)}
    service = DaemonGitService()
    operation = (
        service.run([], cwd=home, timeout=TIMEOUT_SECONDS, env=env)
        if command == b"r"
        else service.stream_bytes([], cwd=home, timeout=TIMEOUT_SECONDS, env=env, consume=None)
    )
    task = asyncio.create_task(operation)
    started_at = time.monotonic()
    try:
        async with asyncio.timeout(5.0):
            while not (home / PID_FILE).exists():
                await asyncio.sleep(0.005)
        pid = int((home / PID_FILE).read_text())
        with (home / BLOCKED).open("wb", buffering=0) as channel:
            channel.write(f"{pid} {started_at}\n".encode())
        # Deliberate loop starvation, confined to this temporary test daemon.
        time.sleep(BLOCK_SECONDS)
        result = await task
        with (home / RESULT).open("wb", buffering=0) as channel:
            channel.write(result.status.encode())
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def main() -> None:
    home = Path(os.environ["GOBBY_HOME"])
    original = runner_lifecycle.run_daemon
    original_kill = daemon_git_module._kill_process_group

    def record_kill(process: _GitProcess) -> None:
        original_kill(process)
        signalled_at = time.monotonic()
        pid_path = home / PID_FILE
        if pid_path.exists() and process.pid == int(pid_path.read_text()):
            (home / KILL_FILE).write_text(str(signalled_at))

    async def run_with_probe(
        runner: GobbyRunner, *, ownership_resolution: PidOwnershipResolution
    ) -> None:
        probe = asyncio.create_task(_reproduce(home))
        try:
            await original(runner, ownership_resolution=ownership_resolution)
        finally:
            probe.cancel()
            await asyncio.gather(probe, return_exceptions=True)

    with (
        patch.object(runner_lifecycle, "run_daemon", run_with_probe),
        patch.object(daemon_git_module, "_kill_process_group", record_kill),
    ):
        runpy.run_module("gobby.runner", run_name="__main__")


if __name__ == "__main__":
    main()
