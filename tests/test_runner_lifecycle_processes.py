"""Child reap keeps the gterm host tree unless the operator asked to drain it (#22002)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import gobby.agents.srt_monitor as srt_monitor
import gobby.runner_lifecycle_processes as runner_lifecycle_processes
from gobby.config.terminal_host import TerminalHostConfig
from gobby.config.terminals import TerminalConfig
from gobby.runner import GobbyRunner
from gobby.terminals.host_manager import TerminalHostManager
from gobby.terminals.host_protocol import write_pidfile

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [300.0, 299.0])
async def test_restart_preservation_checks_monitor_creation_identity(
    monkeypatch: pytest.MonkeyPatch, started: float
) -> None:
    from gobby.agents import srt_monitor

    processes = _install_fake_psutil(monkeypatch)
    monkeypatch.setattr(srt_monitor, "live_monitor_identities", lambda: {WORKER_PID: started})
    await runner_lifecycle_processes._reap_remaining_child_processes(
        preserve_agents=True,
        preserve_srt_monitor=True,
    )
    assert processes[WORKER_PID].terminated is (started != float(WORKER_PID))


@pytest.mark.asyncio
async def test_reap_logs_every_terminated_child_by_identity(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    processes = _install_fake_psutil(monkeypatch)
    caplog.set_level(logging.INFO, logger="gobby.runner_lifecycle")
    await runner_lifecycle_processes._reap_remaining_child_processes()
    for pid in (HOST_PID, SHELL_PID, WORKER_PID):
        assert processes[pid].terminated
        assert f"pid={pid} " in caplog.text


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


def _host_manager(short_socket_dir: Path, *, identity_ok: bool = True) -> TerminalHostManager:
    return TerminalHostManager(
        config=TerminalHostConfig(socket_dir=str(short_socket_dir)),
        terminal_config=TerminalConfig(),
        pid_identity=lambda pid: identity_ok and pid == HOST_PID,
    )


@pytest.mark.asyncio
async def test_reap_preserves_gterm_host_by_default(
    short_socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes = _install_fake_psutil(monkeypatch)
    host = _host_manager(short_socket_dir)
    write_pidfile(short_socket_dir, HOST_PID)
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
async def test_reap_takes_drained_host(
    short_socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes = _install_fake_psutil(monkeypatch)
    host = _host_manager(short_socket_dir)
    write_pidfile(short_socket_dir, HOST_PID)
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


def test_host_preserve_pids_requires_gterm_identity(short_socket_dir: Path) -> None:
    host = _host_manager(short_socket_dir, identity_ok=False)
    write_pidfile(short_socket_dir, HOST_PID)
    host.host_pid = HOST_PID
    runner = SimpleNamespace(terminal_host_manager=host)
    assert runner_lifecycle_processes._host_preserve_pids(cast(GobbyRunner, runner)) == set()

    missing: dict[str, Any] = {}
    assert (
        runner_lifecycle_processes._host_preserve_pids(
            cast(GobbyRunner, SimpleNamespace(**missing))
        )
        == set()
    )


@pytest.mark.asyncio
async def test_reap_enumeration_overrun_names_the_step_and_each_unreaped_child(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    processes = _install_fake_psutil(monkeypatch)
    caplog.set_level(logging.ERROR, logger="gobby.runner_lifecycle")
    release = threading.Event()

    def stalled_identities() -> dict[int, float]:
        release.wait()
        return {}

    monkeypatch.setattr(srt_monitor, "live_monitor_identities", stalled_identities)
    monkeypatch.setattr(
        runner_lifecycle_processes, "_CHILD_ENUMERATION_BUDGET_SECONDS", 0.05, raising=False
    )
    try:
        # Hang guard only: a reaper that ignores its enumeration bound fails here.
        async with asyncio.timeout(30):
            await runner_lifecycle_processes._reap_remaining_child_processes(
                preserve_agents=True,
                preserved_agent_pids=set(),
                preserve_srt_monitor=True,
            )
    finally:
        release.set()
    assert not processes[HOST_PID].terminated
    assert "during live_monitor_identities" in caplog.text
    for pid in (HOST_PID, SHELL_PID, WORKER_PID):
        assert f"pid={pid} " in caplog.text


@pytest.mark.asyncio
async def test_cancelled_reap_finishes_its_kill_sweep_before_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_psutil(monkeypatch)
    fake_psutil = sys.modules["psutil"]
    sweeps: list[list[int]] = []
    kill_sweep_entered = threading.Event()
    release = threading.Event()

    def wait_procs(
        children: list[FakeProcess], timeout: float
    ) -> tuple[list[FakeProcess], list[FakeProcess]]:
        sweeps.append(sorted(child.pid for child in children))
        if len(sweeps) == 1:
            # SIGTERM sweep: nothing exits gracefully, so every child is force-killed.
            return [], list(children)
        kill_sweep_entered.set()
        release.wait()  # the force-kill sweep is delayed here
        return list(children), []

    monkeypatch.setattr(fake_psutil, "wait_procs", staticmethod(wait_procs))
    reap = asyncio.create_task(runner_lifecycle_processes._reap_remaining_child_processes())
    try:
        # Hang guard only: a reaper that never reaches its kill sweep fails here.
        async with asyncio.timeout(30):
            await asyncio.to_thread(kill_sweep_entered.wait)
            reap.cancel()  # the caller gives up while the kill sweep is in flight
            await asyncio.sleep(0)
            assert not reap.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await reap
    assert sweeps[1] == sorted([HOST_PID, SHELL_PID, WORKER_PID])
