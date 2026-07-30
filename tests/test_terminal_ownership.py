from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from gobby.terminal_ownership import (
    OwnershipState,
    PaneOwnershipDecision,
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
) -> SimpleNamespace:
    terminal_context: dict[str, object] = {
        "parent_pid": pid,
        "parent_create_time": create_time if create_time is not None else float(pid),
        "tmux_pane": pane,
        "tmux_socket_name": socket_name,
        "tty": "/dev/ttys001",
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
        foreground_group_factory=lambda _tty: foreground_group,
    )


@pytest.mark.parametrize("source", ["claude", "codex"])
def test_foreground_provider_process_owns_pane(source: str) -> None:
    session = _session(source, 10)

    decision = _resolve([session], _ProcessFactory(_FakeProcess(10, 10.0)), {10: 100})

    assert decision.owner_session_id == source
    assert decision.state is OwnershipState.OWNED
    assert decision.reason == "validated_foreground_process"


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
        foreground_group_factory=lambda _tty: 100,
    )

    assert inspection.state is OwnershipState.INDETERMINATE


def test_terminal_probe_failure_is_indeterminate() -> None:
    session = _session("probe-error", 10)

    def failed_probe(_tty: str) -> int:
        raise OSError("tcgetpgrp failed")

    inspection = inspect_foreground_ownership(
        session,
        process_factory=_ProcessFactory(_FakeProcess(10, 10.0)),
        process_group_factory=lambda _pid: 100,
        foreground_group_factory=failed_probe,
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
