from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import psutil
import pytest

from gobby.terminal_ownership import (
    ForegroundOwnershipInspection,
    OwnershipState,
    foreground_process_group,
    inspect_foreground_ownership,
    recorded_process_exited,
    recorded_seat_left,
)


class _FakeProcess:
    def __init__(
        self,
        pid: int,
        create_time: float,
        *,
        parents: list[_FakeProcess] | None = None,
    ) -> None:
        self.pid = pid
        self._create_time = create_time
        self._parents = parents or []

    def create_time(self) -> float:
        return self._create_time

    def parents(self) -> list[Any]:
        return list(self._parents)


class _ProcessFactory:
    def __init__(self, *processes: _FakeProcess) -> None:
        self._processes = {process.pid: process for process in processes}

    def __call__(self, pid: int) -> _FakeProcess:
        try:
            return self._processes[pid]
        except KeyError as exc:
            raise psutil.NoSuchProcess(pid) from exc


def _session(
    session_id: str,
    pid: int,
    *,
    status: str = "active",
    created_at: str = "2026-01-01T00:00:00+00:00",
    create_time: float | None = None,
    machine_id: str = "machine",
    tty: str | None = "/dev/ttys001",
) -> SimpleNamespace:
    terminal_context: dict[str, object] = {
        "parent_pid": pid,
        "parent_create_time": create_time if create_time is not None else float(pid),
        "tty": tty,
    }
    return SimpleNamespace(
        id=session_id,
        status=status,
        created_at=created_at,
        machine_id=machine_id,
        terminal_context=terminal_context,
    )


def test_matching_process_generation_and_foreground_group_is_owned() -> None:
    process = _FakeProcess(10, 10.0)
    inspection = inspect_foreground_ownership(
        _session("foreground", 10),
        process_factory=_ProcessFactory(process),
        process_group_factory=lambda _pid: 100,
        foreground_group_factory=lambda _pid: 100,
    )

    assert inspection.state is OwnershipState.OWNED
    assert inspection.process is process


def test_process_probe_failure_is_indeterminate() -> None:
    session = _session("denied", 10)

    def denied(_pid: int) -> int:
        raise PermissionError

    inspection = inspect_foreground_ownership(
        session,
        process_factory=_ProcessFactory(_FakeProcess(10, 10.0)),
        process_group_factory=denied,
        foreground_group_factory=lambda _pid: 100,
    )

    assert inspection.state is OwnershipState.INDETERMINATE


@pytest.mark.parametrize(
    "probe_error",
    [
        OSError("ps failed"),
        PermissionError("operation not permitted"),
        subprocess.TimeoutExpired(cmd="ps", timeout=2.0),
    ],
)
def test_terminal_probe_failure_is_indeterminate(probe_error: BaseException) -> None:
    session = _session("probe-error", 10)

    def failed_probe(_pid: int) -> int:
        raise probe_error

    inspection = inspect_foreground_ownership(
        session,
        process_factory=_ProcessFactory(_FakeProcess(10, 10.0)),
        process_group_factory=lambda _pid: 100,
        foreground_group_factory=failed_probe,
    )

    assert inspection.state is OwnershipState.INDETERMINATE


def test_process_disappearance_during_terminal_probe_is_ownerless() -> None:
    session = _session("disappeared", 10)

    def disappeared(_pid: int) -> int:
        raise ProcessLookupError

    inspection = inspect_foreground_ownership(
        session,
        process_factory=_ProcessFactory(_FakeProcess(10, 10.0)),
        process_group_factory=lambda _pid: 100,
        foreground_group_factory=disappeared,
    )

    assert inspection.state is OwnershipState.OWNERLESS


def test_foreground_process_group_reads_tpgid_with_ps() -> None:
    result = subprocess.CompletedProcess(
        args=["ps"],
        returncode=0,
        stdout="  100\n",
        stderr="",
    )
    runner = Mock(return_value=result)

    assert foreground_process_group(42, runner=runner) == 100
    runner.assert_called_once_with(
        ["ps", "-o", "tpgid=", "-p", "42"],
        capture_output=True,
        text=True,
        timeout=2.0,
        check=False,
    )


@pytest.mark.parametrize("output", ["", "not-a-pgid", "100\n101\n"])
def test_malformed_ps_output_is_indeterminate(output: str) -> None:
    session = _session("malformed", 10)
    runner = Mock(
        return_value=subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout=output,
            stderr="",
        )
    )

    inspection = inspect_foreground_ownership(
        session,
        process_factory=_ProcessFactory(_FakeProcess(10, 10.0)),
        process_group_factory=lambda _pid: 100,
        foreground_group_factory=lambda pid: foreground_process_group(pid, runner=runner),
    )

    assert inspection.state is OwnershipState.INDETERMINATE


def _inspector(
    *processes: _FakeProcess, foreground: int = 100
) -> Callable[[object], ForegroundOwnershipInspection]:
    return partial(
        inspect_foreground_ownership,
        process_factory=_ProcessFactory(*processes),
        process_group_factory=lambda _pid: 100,
        foreground_group_factory=lambda _pid: foreground,
    )


def test_recorded_seat_left_when_its_process_is_gone() -> None:
    assert recorded_seat_left(_session("gone", 10), inspect=_inspector()) is True


def test_recorded_seat_left_when_it_lost_the_foreground() -> None:
    inspect = _inspector(_FakeProcess(10, 10.0), foreground=200)

    assert recorded_seat_left(_session("backgrounded", 10), inspect=inspect) is True


def test_recorded_seat_stays_while_it_owns_the_foreground() -> None:
    inspect = _inspector(_FakeProcess(10, 10.0))

    assert recorded_seat_left(_session("owned", 10), inspect=inspect) is False


def test_uninspectable_seat_is_not_reported_gone() -> None:
    session = _session("denied", 10)

    def denied(pid: int) -> _FakeProcess:
        raise psutil.AccessDenied(pid)

    inspect = partial(inspect_foreground_ownership, process_factory=denied)

    assert recorded_seat_left(session, inspect=inspect) is False


@pytest.mark.parametrize(
    "terminal_context",
    [{"cwd": "/tmp"}, {"parent_pid": 10}],
    ids=["no-pid", "pid-without-create-time"],
)
def test_session_without_a_recorded_process_identity_names_no_seat_to_lose(
    terminal_context: dict[str, object],
) -> None:
    session = SimpleNamespace(id="unrecorded", terminal_context=terminal_context)

    assert recorded_seat_left(session, inspect=_inspector()) is False
    assert recorded_process_exited(session) is False


def test_recorded_process_exited_only_when_its_recorded_start_no_longer_runs() -> None:
    running = SimpleNamespace(
        terminal_context={
            "parent_pid": os.getpid(),
            "parent_create_time": psutil.Process().create_time(),
        }
    )
    recycled = SimpleNamespace(
        terminal_context={"parent_pid": os.getpid(), "parent_create_time": 1.0}
    )

    assert recorded_process_exited(running) is False
    assert recorded_process_exited(recycled) is True
