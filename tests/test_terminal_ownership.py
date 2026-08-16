from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import psutil
import pytest

from gobby.terminal_ownership import (
    OwnershipState,
    PaneOwnershipDecision,
    foreground_process_group,
    inspect_foreground_ownership,
    resolve_pane_ownership,
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
    pane: str = "%1",
    socket_name: str = "gobby",
    tty: str | None = "/dev/ttys001",
) -> SimpleNamespace:
    terminal_context: dict[str, object] = {
        "parent_pid": pid,
        "parent_create_time": create_time if create_time is not None else float(pid),
        "tmux_pane": pane,
        "tmux_socket_name": socket_name,
        "tty": tty,
    }
    return SimpleNamespace(
        id=session_id,
        status=status,
        created_at=created_at,
        machine_id=machine_id,
        terminal_context=terminal_context,
    )


def _resolve(
    sessions: list[object],
    processes: _ProcessFactory,
    process_groups: dict[int, int],
    *,
    foreground_group: int = 100,
    requested_session_id: str | None = None,
) -> PaneOwnershipDecision:
    return resolve_pane_ownership(
        sessions,
        requested_session_id=requested_session_id,
        process_factory=processes,
        process_group_factory=process_groups.__getitem__,
        foreground_group_factory=lambda _pid: foreground_group,
    )


@pytest.mark.parametrize("source", ["claude", "codex"])
def test_foreground_provider_process_owns_pane(source: str) -> None:
    session = _session(source, 10)

    decision = _resolve([session], _ProcessFactory(_FakeProcess(10, 10.0)), {10: 100})

    assert decision.owner_session_id == source
    assert decision.state is OwnershipState.OWNED
    assert decision.reason == "validated_foreground_process"


def test_live_session_without_tty_owns_pane() -> None:
    session = _session("codex", 10, tty=None)

    decision = _resolve([session], _ProcessFactory(_FakeProcess(10, 10.0)), {10: 100})

    assert decision.owner_session_id == "codex"
    assert decision.state is OwnershipState.OWNED


def test_handoff_ready_is_eligible_foreground_owner() -> None:
    session = _session("handoff", 10, status="handoff_ready")

    decision = _resolve([session], _ProcessFactory(_FakeProcess(10, 10.0)), {10: 100})

    assert decision.owner_session_id == "handoff"


def test_expired_and_deleted_rows_cannot_displace_valid_owner() -> None:
    active = _session("active", 10)
    expired = _session("expired", 20, status="expired")
    deleted = _session("deleted", 30, status="deleted")

    decision = _resolve(
        [expired, deleted, active],
        _ProcessFactory(
            _FakeProcess(10, 10.0),
            _FakeProcess(20, 20.0),
            _FakeProcess(30, 30.0),
        ),
        {10: 100, 20: 100, 30: 100},
    )

    assert decision.owner_session_id == "active"
    assert decision.validated_session_ids == frozenset({"active"})


def test_same_pid_active_owner_beats_newer_handoff_ready() -> None:
    older = _session("older", 10, created_at="2026-01-01T00:00:00+00:00")
    newer = _session(
        "newer",
        10,
        status="handoff_ready",
        created_at="2026-01-01T00:01:00+00:00",
    )

    decision = _resolve(
        [older, newer],
        _ProcessFactory(_FakeProcess(10, 10.0)),
        {10: 100},
        requested_session_id="older",
    )

    assert decision.owner_session_id == "older"
    assert decision.validated_session_ids == frozenset({"older", "newer"})


def test_same_pid_expired_request_cannot_displace_active_owner() -> None:
    older = _session("older", 10, created_at="2026-01-01T00:00:00+00:00")
    newer = _session(
        "newer",
        10,
        status="expired",
        created_at="2026-01-01T00:01:00+00:00",
    )

    decision = _resolve(
        [older, newer],
        _ProcessFactory(_FakeProcess(10, 10.0)),
        {10: 100},
        requested_session_id="newer",
    )

    assert decision.owner_session_id == "older"
    assert decision.validated_session_ids == frozenset({"older", "newer"})


def test_same_pid_requested_active_beats_older_active_sibling() -> None:
    older = _session("older", 10, created_at="2026-01-01T00:00:00+00:00")
    newer = _session("newer", 10, created_at="2026-01-01T00:01:00+00:00")

    decision = _resolve(
        [older, newer],
        _ProcessFactory(_FakeProcess(10, 10.0)),
        {10: 100},
        requested_session_id="newer",
    )

    assert decision.owner_session_id == "newer"


@pytest.mark.parametrize("status", ["expired", "deleted"])
def test_inactive_row_is_ownerless(status: str) -> None:
    session = _session(status, 10, status=status)

    decision = _resolve([session], _ProcessFactory(_FakeProcess(10, 10.0)), {10: 100})

    assert decision.owner is None
    assert decision.state is OwnershipState.OWNERLESS


def test_background_provider_process_is_ownerless() -> None:
    session = _session("background", 10)

    decision = _resolve([session], _ProcessFactory(_FakeProcess(10, 10.0)), {10: 200})

    assert decision.owner is None
    assert decision.state is OwnershipState.OWNERLESS


def test_dead_pid_is_ownerless() -> None:
    session = _session("dead", 10)

    decision = _resolve([session], _ProcessFactory(), {})

    assert decision.owner is None
    assert decision.state is OwnershipState.OWNERLESS


def test_pid_reuse_with_mismatched_creation_time_is_ownerless() -> None:
    session = _session("reused", 10, create_time=10.0)

    decision = _resolve([session], _ProcessFactory(_FakeProcess(10, 99.0)), {10: 100})

    assert decision.owner is None
    assert decision.state is OwnershipState.OWNERLESS


def test_nested_foreground_processes_select_outermost_provider() -> None:
    outer_process = _FakeProcess(10, 10.0)
    inner_process = _FakeProcess(20, 20.0, parents=[outer_process])
    outer = _session("outer", 10)
    inner = _session("inner", 20)

    decision = _resolve(
        [inner, outer],
        _ProcessFactory(inner_process, outer_process),
        {10: 100, 20: 100},
    )

    assert decision.owner_session_id == "outer"
    assert decision.reason == "nested_outermost_process"
    assert decision.validated_session_ids == frozenset({"inner", "outer"})


def test_unrelated_foreground_processes_are_indeterminate() -> None:
    first = _session("first", 10)
    second = _session("second", 20)

    decision = _resolve(
        [first, second],
        _ProcessFactory(_FakeProcess(10, 10.0), _FakeProcess(20, 20.0)),
        {10: 100, 20: 100},
    )

    assert decision.owner is None
    assert decision.state is OwnershipState.INDETERMINATE
    assert decision.reason == "ambiguous_foreground_processes"


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


def test_distinct_terminal_identities_cannot_be_resolved_together() -> None:
    first = _session("first", 10, pane="%1")
    second = _session("second", 20, pane="%2")

    decision = _resolve(
        [first, second],
        _ProcessFactory(_FakeProcess(10, 10.0), _FakeProcess(20, 20.0)),
        {10: 100, 20: 100},
    )

    assert decision.reason == "invalid_identity"
    assert decision.owner is None
