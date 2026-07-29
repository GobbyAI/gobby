"""Canonical tmux pane ownership selection tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from gobby.terminal_ownership import resolve_pane_ownership

pytestmark = pytest.mark.unit


@dataclass
class _FakeProcess:
    pid: int
    born_at: float
    ancestor_pids: tuple[int, ...] = ()

    def create_time(self) -> float:
        return self.born_at

    def parents(self) -> list[Any]:
        return [SimpleNamespace(pid=pid) for pid in self.ancestor_pids]


class _ProcessFactory:
    def __init__(self, *processes: _FakeProcess) -> None:
        self.processes = {process.pid: process for process in processes}

    def __call__(self, pid: int) -> _FakeProcess:
        try:
            return self.processes[pid]
        except KeyError as exc:
            raise psutil.NoSuchProcess(pid) from exc


def _session(
    session_id: str,
    *,
    pid: int,
    create_time: float,
    status: str = "active",
    machine_id: str = "machine-1",
    socket: str = "/tmp/tmux-gobby",
    pane: str = "%226",
    created_offset: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=session_id,
        status=status,
        machine_id=machine_id,
        created_at=datetime(2026, 7, 28, tzinfo=UTC) + timedelta(seconds=created_offset),
        terminal_context={
            "tmux_socket_path": socket,
            "tmux_pane": pane,
            "parent_pid": pid,
            "parent_create_time": create_time,
        },
    )


def test_nested_live_processes_select_outermost_provider() -> None:
    codex = _session("codex", pid=100, create_time=10.0, status="expired")
    grok = _session("grok", pid=200, create_time=20.0, status="paused")
    processes = _ProcessFactory(
        _FakeProcess(100, 10.0),
        _FakeProcess(200, 20.0, ancestor_pids=(150, 100)),
    )

    decision = resolve_pane_ownership(
        [grok, codex],
        requested_session_id="grok",
        process_factory=processes,
    )

    assert decision.owner is codex
    assert decision.reason == "nested_outermost_process"
    assert decision.validated_session_ids == {"codex", "grok"}
    assert decision.requested_session_owns_pane is False


def test_dead_nested_child_does_not_displace_live_expired_parent() -> None:
    codex = _session("codex", pid=100, create_time=10.0, status="expired")
    grok = _session("grok", pid=200, create_time=20.0, status="paused")

    decision = resolve_pane_ownership(
        [grok, codex],
        requested_session_id="codex",
        process_factory=_ProcessFactory(_FakeProcess(100, 10.0)),
    )

    assert decision.owner is codex
    assert decision.reason == "validated_live_process"
    assert decision.validated_session_ids == {"codex"}
    assert decision.requested_session_owns_pane is True


def test_unrelated_live_processes_are_ambiguous() -> None:
    codex = _session("codex", pid=100, create_time=10.0)
    qwen = _session("qwen", pid=300, create_time=30.0)

    decision = resolve_pane_ownership(
        [codex, qwen],
        process_factory=_ProcessFactory(
            _FakeProcess(100, 10.0),
            _FakeProcess(300, 30.0),
        ),
    )

    assert decision.owner is None
    assert decision.reason == "ambiguous_live_processes"


def test_pid_reuse_with_mismatched_creation_time_is_not_live() -> None:
    stale = _session("stale", pid=100, create_time=10.0, status="expired")

    decision = resolve_pane_ownership(
        [stale],
        process_factory=_ProcessFactory(_FakeProcess(100, 99.0)),
    )

    assert decision.owner is None
    assert decision.reason == "ownerless"


def test_exactly_one_active_or_paused_record_is_fallback_owner() -> None:
    paused = _session("paused", pid=100, create_time=10.0, status="paused")
    expired = _session("expired", pid=200, create_time=20.0, status="expired")

    decision = resolve_pane_ownership(
        [expired, paused],
        requested_session_id="paused",
        process_factory=_ProcessFactory(),
    )

    assert decision.owner is paused
    assert decision.reason == "single_lifecycle_fallback"
    assert decision.requested_session_owns_pane is True


def test_multiple_lifecycle_fallback_candidates_are_ambiguous() -> None:
    active = _session("active", pid=100, create_time=10.0)
    paused = _session("paused", pid=200, create_time=20.0, status="paused")

    decision = resolve_pane_ownership(
        [active, paused],
        process_factory=_ProcessFactory(),
    )

    assert decision.owner is None
    assert decision.reason == "ambiguous_lifecycle_fallback"


def test_requested_session_absent_from_candidates_does_not_own_pane() -> None:
    owner = _session("owner", pid=100, create_time=10.0)

    decision = resolve_pane_ownership(
        [owner],
        requested_session_id="missing",
        process_factory=_ProcessFactory(_FakeProcess(100, 10.0)),
    )

    assert decision.owner is owner
    assert decision.reason == "validated_live_process"
    assert decision.requested_session_owns_pane is False


def test_distinct_machine_socket_or_pane_identities_cannot_be_resolved_together() -> None:
    root = _session("root", pid=100, create_time=10.0)
    other_machine = _session(
        "other-machine",
        pid=200,
        create_time=20.0,
        machine_id="machine-2",
    )
    other_socket = _session(
        "other-socket",
        pid=300,
        create_time=30.0,
        socket="/tmp/other",
    )
    other_pane = _session("other-pane", pid=400, create_time=40.0, pane="%227")

    for contender in (other_machine, other_socket, other_pane):
        decision = resolve_pane_ownership(
            [root, contender],
            process_factory=_ProcessFactory(),
        )
        assert decision.owner is None
        assert decision.reason == "invalid_identity"
