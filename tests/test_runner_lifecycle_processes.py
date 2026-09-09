"""Child reap keeps the gterm host tree unless the operator asked to drain it (#22002)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import gobby.runner_lifecycle_processes as runner_lifecycle_processes
from gobby.config.terminal_host import TerminalHostConfig
from gobby.config.terminals import TerminalConfig
from gobby.runner import GobbyRunner
from gobby.terminals.host_manager import TerminalHostManager
from gobby.terminals.host_protocol import write_pidfile

pytestmark = pytest.mark.unit

HOST_PID = 500
SHELL_PID = 501
WORKER_PID = 300


class FakeProcess:
    def __init__(
        self,
        pid: int,
        name: str,
        children: list[FakeProcess] | None = None,
    ) -> None:
        self.pid = pid
        self._name = name
        self._children = children or []
        self._parent: FakeProcess | None = None
        self.terminated = False
        for child in self._children:
            child._parent = self

    def children(self, recursive: bool = False) -> list[FakeProcess]:
        if not recursive:
            return list(self._children)
        result: list[FakeProcess] = []
        pending = list(self._children)
        while pending:
            child = pending.pop(0)
            result.append(child)
            pending.extend(child._children)
        return result

    def parent(self) -> FakeProcess | None:
        return self._parent

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        return [self._name]

    def create_time(self) -> float:
        return float(self.pid)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


def _install_fake_psutil(monkeypatch: pytest.MonkeyPatch) -> dict[int, FakeProcess]:
    shell = FakeProcess(SHELL_PID, "zsh")
    host = FakeProcess(HOST_PID, "gterm", [shell])
    worker = FakeProcess(WORKER_PID, "gcode")
    current = FakeProcess(os.getpid(), "python", [host, worker])
    processes = {process.pid: process for process in [current, host, shell, worker]}

    class FakeNoSuchProcess(Exception):
        pass

    class FakeAccessDenied(Exception):
        pass

    class FakePsutil:
        NoSuchProcess = FakeNoSuchProcess
        AccessDenied = FakeAccessDenied

        @staticmethod
        def Process(pid: int) -> FakeProcess:
            return processes[pid]

        @staticmethod
        def wait_procs(
            children: list[FakeProcess], timeout: float
        ) -> tuple[list[FakeProcess], list[FakeProcess]]:
            return children, []

    monkeypatch.setitem(sys.modules, "psutil", FakePsutil)
    return processes


def _host_manager(tmp_path: Path, *, identity_ok: bool = True) -> TerminalHostManager:
    return TerminalHostManager(
        config=TerminalHostConfig(socket_dir=str(tmp_path)),
        terminal_config=TerminalConfig(),
        pid_identity=lambda pid: identity_ok and pid == HOST_PID,
    )


@pytest.mark.asyncio
async def test_reap_preserves_gterm_host_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes = _install_fake_psutil(monkeypatch)
    host = _host_manager(tmp_path)
    write_pidfile(tmp_path, HOST_PID)
    # `stop()` has already cleared the supervisor's pid; the pidfile still names the host.
    host.host_pid = None
    runner = SimpleNamespace(terminal_host_manager=host)

    preserved = runner_lifecycle_processes._host_preserve_pids(cast(GobbyRunner, runner))
    assert preserved == {HOST_PID}

    await runner_lifecycle_processes._reap_remaining_child_processes(
        preserve_agents=True,
        preserved_agent_pids=preserved,
    )

    assert processes[HOST_PID].terminated is False
    assert processes[SHELL_PID].terminated is False, "host descendants survive too"
    assert processes[WORKER_PID].terminated is True


@pytest.mark.asyncio
async def test_reap_takes_drained_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    processes = _install_fake_psutil(monkeypatch)
    host = _host_manager(tmp_path)
    write_pidfile(tmp_path, HOST_PID)
    host.host_pid = HOST_PID
    host.host_drained = True
    runner = SimpleNamespace(terminal_host_manager=host)

    preserved = runner_lifecycle_processes._host_preserve_pids(cast(GobbyRunner, runner))
    assert preserved == set()

    await runner_lifecycle_processes._reap_remaining_child_processes(
        preserve_agents=True,
        preserved_agent_pids=preserved,
    )
    assert processes[HOST_PID].terminated is True
    assert processes[SHELL_PID].terminated is True


def test_host_preserve_pids_requires_gterm_identity(tmp_path: Path) -> None:
    host = _host_manager(tmp_path, identity_ok=False)
    write_pidfile(tmp_path, HOST_PID)
    host.host_pid = HOST_PID
    runner = SimpleNamespace(terminal_host_manager=host)
    assert runner_lifecycle_processes._host_preserve_pids(cast(GobbyRunner, runner)) == set()

    missing: dict[str, Any] = {}
    assert (
        runner_lifecycle_processes._host_preserve_pids(cast(GobbyRunner, SimpleNamespace(**missing)))
        == set()
    )
