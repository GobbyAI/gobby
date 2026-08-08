"""Tests for the tmux window-name repair maintenance loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

from gobby.agents.tmux.session_manager import TmuxProbeResult, TmuxProbeState
from gobby.runner_maintenance import (
    _select_tmux_repair_sessions,
    _tmux_repair_candidate_score,
    _tmux_repair_pane_key,
    tmux_window_name_repair_loop,
)

pytestmark = pytest.mark.unit


class _SessionManager:
    def __init__(self, sessions: list[SimpleNamespace]) -> None:
        for session in sessions:
            if not hasattr(session, "machine_id"):
                session.machine_id = "machine"
        self.sessions = sessions
        self.calls: list[tuple[list[str], int]] = []
        self.socket_expirations: list[tuple[str, str]] = []
        self.pane_expirations: list[tuple[str, str, str]] = []
        self.affected_ids: list[str] = []

    def list(self, *, statuses: list[str], limit: int) -> Sequence[SimpleNamespace]:
        self.calls.append((statuses, limit))
        return self.sessions

    def expire_tmux_socket_sessions(self, machine_id: str, socket_identity: str) -> Sequence[str]:
        self.socket_expirations.append((machine_id, socket_identity))
        return self.affected_ids

    def expire_tmux_pane_sessions(
        self, machine_id: str, socket_identity: str, pane: str
    ) -> Sequence[str]:
        self.pane_expirations.append((machine_id, socket_identity, pane))
        return self.affected_ids


@pytest.fixture(autouse=True)
def _live_tmux_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def probe(_session: SimpleNamespace) -> TmuxProbeResult:
        return TmuxProbeResult(TmuxProbeState.LIVE, True)

    monkeypatch.setattr("gobby.runner_maintenance.isolation.probe_tmux_pane", probe)


class _BrokenSessionManager(_SessionManager):
    def __init__(self) -> None:
        super().__init__([])

    def list(self, *, statuses: list[str], limit: int) -> Sequence[SimpleNamespace]:
        self.calls.append((statuses, limit))
        raise RuntimeError("db down")


def test_tmux_repair_pane_key_uses_socket_identity() -> None:
    session = SimpleNamespace(
        machine_id="machine",
        terminal_context={"tmux_pane": "%1", "tmux_socket_name": "sock"},
    )
    assert _tmux_repair_pane_key(session) == (
        "machine",
        "tmux_socket_name:sock",
        "%1",
    )


def test_tmux_repair_candidate_score_prefers_identity_and_activity() -> None:
    inactive = SimpleNamespace(external_id="", message_count=0, turn_count=0, tool_call_count=0)
    active = SimpleNamespace(external_id="external", message_count=1)

    assert _tmux_repair_candidate_score(inactive) == (0, 0)
    assert _tmux_repair_candidate_score(active) == (1, 1)


@pytest.mark.asyncio
async def test_repair_loop_skips_sessions_without_machine_identity() -> None:
    session = SimpleNamespace(
        id="missing-machine",
        ref="#1",
        machine_id="",
        external_id="external",
        terminal_context={"tmux_pane": "%1", "tmux_socket_name": "gobby"},
        message_count=1,
        turn_count=0,
        tool_call_count=0,
    )
    manager = _SessionManager([session])

    with patch(
        "gobby.runner_maintenance.isolation.probe_tmux_pane",
        new=AsyncMock(),
    ) as probe:
        await tmux_window_name_repair_loop(manager, lambda: True)

    probe.assert_not_awaited()
    assert manager.socket_expirations == []
    assert manager.pane_expirations == []


def test_select_tmux_repair_sessions_keeps_best_candidate_per_pane() -> None:
    stale = SimpleNamespace(
        external_id="",
        terminal_context={"tmux_pane": "%1", "tmux_socket_path": "/tmp/tmux"},
        message_count=0,
    )
    best = SimpleNamespace(
        external_id="external",
        terminal_context={"tmux_pane": "%1", "tmux_socket_path": "/tmp/tmux"},
        message_count=1,
    )
    other = SimpleNamespace(
        external_id="other",
        terminal_context={"tmux_pane": "%2", "tmux_socket_path": "/tmp/tmux"},
        message_count=0,
    )

    assert _select_tmux_repair_sessions([stale, best, other]) == [best, other]


def test_select_tmux_repair_sessions_uses_creation_order_after_quality() -> None:
    created_at = datetime.now(UTC)
    older = SimpleNamespace(
        id="older",
        seq_num=9596,
        created_at=created_at,
        updated_at=created_at + timedelta(minutes=10),
        machine_id="machine",
        external_id="old-external",
        terminal_context={
            "tmux_pane": "%154",
            "tmux_socket_path": "/tmp/tmux-501/default",
        },
        transcript_path="/tmp/old.jsonl",
        message_count=1,
        turn_count=1,
        tool_call_count=1,
    )
    newer = SimpleNamespace(
        id="newer",
        seq_num=9597,
        created_at=created_at + timedelta(seconds=1),
        updated_at=created_at + timedelta(seconds=1),
        machine_id="machine",
        external_id="new-external",
        terminal_context={
            "tmux_pane": "%154",
            "tmux_socket_path": "/tmp/tmux-501/default",
        },
        transcript_path="/tmp/new.jsonl",
        message_count=1,
        turn_count=1,
        tool_call_count=1,
    )

    assert _select_tmux_repair_sessions([older, newer]) == [newer]


def test_select_tmux_repair_sessions_keeps_distinct_machines_and_sockets() -> None:
    common = {
        "external_id": "external",
        "transcript_path": "/tmp/transcript.jsonl",
        "message_count": 1,
        "turn_count": 1,
        "tool_call_count": 1,
    }
    first = SimpleNamespace(
        **common,
        machine_id="machine-a",
        terminal_context={"tmux_pane": "%154", "tmux_socket_name": "default"},
    )
    other_machine = SimpleNamespace(
        **common,
        machine_id="machine-b",
        terminal_context={"tmux_pane": "%154", "tmux_socket_name": "default"},
    )
    other_socket = SimpleNamespace(
        **common,
        machine_id="machine-a",
        terminal_context={"tmux_pane": "%154", "tmux_socket_name": "other"},
    )

    assert _select_tmux_repair_sessions([first, other_machine, other_socket]) == [
        first,
        other_machine,
        other_socket,
    ]


@pytest.mark.asyncio
async def test_repair_loop_enforces_only_paned_sessions() -> None:
    """The sweep lists active/paused sessions and only enforces those with a pane."""
    paned = SimpleNamespace(terminal_context={"tmux_pane": "%1"}, ref="#1")
    no_pane = SimpleNamespace(terminal_context={"cwd": "/x"}, ref="#2")
    none_ctx = SimpleNamespace(terminal_context=None, ref="#3")
    session_manager = _SessionManager([paned, no_pane, none_ctx])

    enforce = AsyncMock(return_value=True)
    owner = AsyncMock(side_effect=lambda session: session)
    with (
        patch("gobby.runner_maintenance.isolation.resolve_tmux_repair_owner", owner),
        patch("gobby.runner_maintenance.isolation.enforce_window_name_if_unmanaged", enforce),
    ):
        # is_shutdown_requested True -> startup repair runs once, then the loop exits.
        await tmux_window_name_repair_loop(session_manager, lambda: True)

    assert session_manager.calls == [
        (["active", "paused", "handoff_ready", "expired", "deleted"], 200)
    ]
    assert owner.await_args_list == [call(paned)]
    enforce.assert_awaited_once_with(paned)


@pytest.mark.asyncio
async def test_repair_loop_repairs_one_best_session_per_tmux_pane() -> None:
    """Duplicate records for one pane cannot fight over the tmux window title."""
    stale = SimpleNamespace(
        external_id="",
        terminal_context={"tmux_pane": "%72", "tmux_socket_path": "/tmp/tmux"},
        transcript_path=None,
        message_count=0,
        turn_count=0,
        tool_call_count=0,
        ref="#7460",
    )
    grok = SimpleNamespace(
        external_id="grok-session-123",
        terminal_context={"tmux_pane": "%72", "tmux_socket_path": "/tmp/tmux"},
        transcript_path="/tmp/grok.jsonl",
        message_count=1,
        turn_count=0,
        tool_call_count=0,
        ref="#7514",
    )
    other = SimpleNamespace(
        external_id="other-session",
        terminal_context={"tmux_pane": "%73", "tmux_socket_path": "/tmp/tmux"},
        transcript_path=None,
        message_count=0,
        turn_count=0,
        tool_call_count=0,
        ref="#7515",
    )
    session_manager = _SessionManager([stale, grok, other])

    enforce = AsyncMock(return_value=True)
    owner = AsyncMock(side_effect=lambda session: session)
    with (
        patch("gobby.runner_maintenance.isolation.resolve_tmux_repair_owner", owner),
        patch("gobby.runner_maintenance.isolation.enforce_window_name_if_unmanaged", enforce),
    ):
        await tmux_window_name_repair_loop(session_manager, lambda: True)

    assert session_manager.calls == [
        (["active", "paused", "handoff_ready", "expired", "deleted"], 200)
    ]
    assert enforce.await_args_list == [call(grok), call(other)]


@pytest.mark.asyncio
async def test_repair_loop_releases_ownerless_inactive_title(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stale = SimpleNamespace(
        id="stale",
        status="expired",
        title="#8151 Frozen title",
        terminal_context={"tmux_pane": "%81", "tmux_socket_path": "/tmp/tmux"},
        ref="#8151",
    )
    session_manager = _SessionManager([stale])

    owner = AsyncMock(return_value=None)
    enforce = AsyncMock()
    release = AsyncMock(return_value=True)
    with (
        caplog.at_level("DEBUG", logger="gobby.runner_maintenance"),
        patch("gobby.runner_maintenance.isolation.resolve_tmux_repair_owner", owner),
        patch("gobby.runner_maintenance.isolation.enforce_window_name_if_unmanaged", enforce),
        patch("gobby.runner_maintenance.isolation.release_window_name_if_unowned", release),
    ):
        await tmux_window_name_repair_loop(session_manager, lambda: True)

    enforce.assert_not_awaited()
    release.assert_awaited_once_with(stale)
    repair_records = [
        record
        for record in caplog.records
        if record.getMessage() == "tmux window repair: renamed 1 window(s)"
    ]
    assert len(repair_records) == 1
    assert repair_records[0].levelname == "DEBUG"


@pytest.mark.asyncio
async def test_repair_loop_enforces_resolved_owner() -> None:
    parent = SimpleNamespace(
        id="codex-parent",
        status="expired",
        external_id="codex-session",
        terminal_context={
            "tmux_pane": "%226",
            "tmux_socket_path": "/tmp/tmux-501/gobby",
        },
        transcript_path="/tmp/codex.jsonl",
        message_count=1,
        turn_count=1,
        tool_call_count=1,
        ref="#9790",
    )
    child = SimpleNamespace(
        id="grok-child",
        status="paused",
        external_id="grok-session",
        terminal_context={
            "tmux_pane": "%226",
            "tmux_socket_path": "/tmp/tmux-501/gobby",
        },
        transcript_path="/tmp/grok.jsonl",
        message_count=2,
        turn_count=2,
        tool_call_count=2,
        ref="#1",
    )
    session_manager = _SessionManager([parent, child])
    owner = AsyncMock(return_value=parent)
    enforce = AsyncMock(return_value=True)

    with (
        patch("gobby.runner_maintenance.isolation.resolve_tmux_repair_owner", owner),
        patch("gobby.runner_maintenance.isolation.enforce_window_name_if_unmanaged", enforce),
    ):
        await tmux_window_name_repair_loop(session_manager, lambda: True)

    assert owner.await_args_list == [call(child)]
    assert enforce.await_args_list == [call(parent)]
    owner.assert_awaited_once_with(child)
    enforce.assert_awaited_once_with(parent)


@pytest.mark.asyncio
async def test_repair_loop_scopes_missing_socket_to_effective_default() -> None:
    """Root and agent default tmux sockets are distinct, but agent depths share one socket."""
    root = SimpleNamespace(
        agent_depth=0,
        external_id="root-session",
        terminal_context={"tmux_pane": "%72"},
        transcript_path="/tmp/root.jsonl",
        message_count=1,
        turn_count=0,
        tool_call_count=0,
        ref="#7600",
    )
    shallow_agent = SimpleNamespace(
        agent_depth=1,
        external_id="",
        terminal_context={"tmux_pane": "%72"},
        transcript_path=None,
        message_count=0,
        turn_count=0,
        tool_call_count=0,
        ref="#7601",
    )
    nested_agent = SimpleNamespace(
        agent_depth=3,
        external_id="nested-agent-session",
        terminal_context={"tmux_pane": "%72"},
        transcript_path="/tmp/nested.jsonl",
        message_count=1,
        turn_count=0,
        tool_call_count=0,
        ref="#7602",
    )
    session_manager = _SessionManager([root, shallow_agent, nested_agent])

    enforce = AsyncMock(return_value=True)
    owner = AsyncMock(side_effect=lambda session: session)
    with (
        patch("gobby.runner_maintenance.isolation.resolve_tmux_repair_owner", owner),
        patch("gobby.runner_maintenance.isolation.enforce_window_name_if_unmanaged", enforce),
    ):
        await tmux_window_name_repair_loop(session_manager, lambda: True)

    assert owner.await_count == 2
    assert enforce.await_args_list == [call(root), call(nested_agent)]


@pytest.mark.asyncio
async def test_repair_loop_cleans_missing_socket_once_without_per_pane_calls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sessions = [
        SimpleNamespace(
            id=f"session-{index}",
            machine_id="machine-a",
            agent_depth=0,
            external_id=f"external-{index}",
            terminal_context={
                "tmux_pane": f"%{index}",
                "tmux_socket_path": "/private/tmp/tmux-501/gobby",
            },
            transcript_path=f"/tmp/{index}.jsonl",
            message_count=1,
            turn_count=0,
            tool_call_count=0,
            ref=f"#{index}",
        )
        for index in range(3)
    ]
    manager = _SessionManager(sessions)
    manager.affected_ids = [session.id for session in sessions]
    missing = AsyncMock(
        return_value=TmuxProbeResult(
            TmuxProbeState.SERVER_MISSING,
            None,
            "error connecting to /private/tmp/tmux-501/gobby (No such file or directory)",
        )
    )
    owner = AsyncMock()

    with (
        patch("gobby.runner_maintenance.isolation.probe_tmux_pane", missing),
        patch("gobby.runner_maintenance.isolation.resolve_tmux_repair_owner", owner),
        caplog.at_level("INFO", logger="gobby.runner_maintenance"),
    ):
        await tmux_window_name_repair_loop(manager, lambda: True)

    assert manager.socket_expirations == [
        ("machine-a", "tmux_socket_path:/private/tmp/tmux-501/gobby")
    ]
    assert manager.pane_expirations == []
    assert missing.await_count == 1
    owner.assert_not_awaited()
    cleanup_logs = [record for record in caplog.records if "missing server" in record.message]
    assert len(cleanup_logs) == 1
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_repair_loop_cleans_missing_pane_and_preserves_indeterminate_probe() -> None:
    session = SimpleNamespace(
        id="session-pane",
        machine_id="machine-a",
        agent_depth=0,
        external_id="external-pane",
        terminal_context={
            "tmux_pane": "%41",
            "tmux_socket_path": "/private/tmp/tmux-501/gobby",
        },
        transcript_path="/tmp/pane.jsonl",
        message_count=1,
        turn_count=0,
        tool_call_count=0,
        ref="#41",
    )
    manager = _SessionManager([session])
    manager.affected_ids = [session.id]

    with patch(
        "gobby.runner_maintenance.isolation.probe_tmux_pane",
        AsyncMock(return_value=TmuxProbeResult(TmuxProbeState.LIVE, False, "can't find pane")),
    ):
        await tmux_window_name_repair_loop(manager, lambda: True)

    assert manager.pane_expirations == [
        ("machine-a", "tmux_socket_path:/private/tmp/tmux-501/gobby", "%41")
    ]

    manager.pane_expirations.clear()
    with patch(
        "gobby.runner_maintenance.isolation.probe_tmux_pane",
        AsyncMock(return_value=TmuxProbeResult(TmuxProbeState.INDETERMINATE, None, "timeout")),
    ):
        await tmux_window_name_repair_loop(manager, lambda: True)

    assert manager.socket_expirations == []
    assert manager.pane_expirations == []


@pytest.mark.asyncio
async def test_repair_loop_uses_configured_session_list_limit() -> None:
    """The repair sweep honors the configured session list limit."""
    session_manager = _SessionManager([])

    await tmux_window_name_repair_loop(
        session_manager,
        lambda: True,
        session_list_limit=50,
    )

    assert session_manager.calls == [
        (["active", "paused", "handoff_ready", "expired", "deleted"], 50)
    ]


@pytest.mark.asyncio
async def test_repair_loop_normalizes_nonpositive_session_list_limit() -> None:
    """Nonpositive limits clamp to the smallest safe list bound."""
    session_manager = _SessionManager([])

    await tmux_window_name_repair_loop(
        session_manager,
        lambda: True,
        session_list_limit=0,
    )

    assert session_manager.calls == [
        (["active", "paused", "handoff_ready", "expired", "deleted"], 1)
    ]


@pytest.mark.asyncio
async def test_repair_loop_normalizes_nonpositive_interval_seconds() -> None:
    """Nonpositive intervals clamp so the repair loop cannot hot-loop."""
    session_manager = _SessionManager([])
    sleep_calls: list[float] = []
    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return shutdown_checks > 1

    async def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise asyncio.CancelledError

    with patch("gobby.runner_maintenance.asyncio.sleep", sleep):
        await tmux_window_name_repair_loop(
            session_manager,
            is_shutdown_requested,
            interval_seconds=0,
        )

    assert sleep_calls == [1]


@pytest.mark.asyncio
async def test_repair_loop_handles_no_session_manager() -> None:
    """A missing session manager is a no-op, not a crash."""
    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return True

    await tmux_window_name_repair_loop(None, is_shutdown_requested)

    assert shutdown_checks == 1


@pytest.mark.asyncio
async def test_repair_loop_survives_list_failure(caplog: pytest.LogCaptureFixture) -> None:
    """A failing session list is logged and does not raise."""
    session_manager = _BrokenSessionManager()

    with caplog.at_level("WARNING", logger="gobby.runner_maintenance"):
        await tmux_window_name_repair_loop(session_manager, lambda: True)

    assert session_manager.calls == [
        (["active", "paused", "handoff_ready", "expired", "deleted"], 200)
    ]
    assert "tmux window repair: failed to list sessions: db down" in caplog.text
