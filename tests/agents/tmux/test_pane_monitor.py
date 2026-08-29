"""Tests for TmuxPaneMonitor."""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.tmux.pane_monitor import _RECENTLY_ENDED_TTL, TmuxPaneMonitor
from gobby.agents.tmux.session_activation import TMUX_COMMAND_TIMEOUT_SECONDS
from gobby.agents.tmux.session_manager import TmuxSessionInfo
from gobby.hooks.events import HookEvent, HookEventType
from gobby.storage.agents import AgentRun
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.terminals.fakes import FakeRuntime, runtime_registry

DETECTION_REGISTRY = BundledDetectionRegistry()
pytestmark = pytest.mark.unit


def _make_agent_run(
    run_id: str = "run-1",
    child_session_id: str = "sess-1",
    parent_session_id: str = "parent-1",
    terminal_id: str | None = "gobby-agent-1",
    pid: int | None = None,
) -> AgentRun:
    return AgentRun(
        id=run_id,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        provider="test",
        prompt="test",
        status="running",
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
        terminal_id=terminal_id,
        pid=pid,
    )


def _make_session_obj(
    session_id: str = "sess-1",
    external_id: str = "ext-1",
    source: str = "claude",
) -> MagicMock:
    s = MagicMock()
    s.id = session_id
    s.external_id = external_id
    s.source = source
    return s


@pytest.fixture(autouse=True)
def _terminal_rows_from_legacy_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.terminals.fakes import make_memory_terminal

    def get(_self: object, terminal_id: str) -> object:
        return make_memory_terminal(terminal_id=terminal_id, session_name=terminal_id)

    monkeypatch.setattr("gobby.storage.terminals.TerminalManager.get", get)


def _make_monitor_with_db(callback: MagicMock) -> TmuxPaneMonitor:
    """Create a TmuxPaneMonitor with a mock session manager."""
    mock_db = MagicMock()
    mock_session_manager = MagicMock()
    mock_session_manager.db = mock_db
    monitor = TmuxPaneMonitor(
        detection_registry=DETECTION_REGISTRY,
        session_end_callback=callback,
        poll_interval=1.0,
        session_manager=mock_session_manager,
        registry=runtime_registry(FakeRuntime()),
    )
    return monitor


@pytest.mark.asyncio
async def test_no_tmux_agents_noop() -> None:
    """When no agents have terminal_id, callback is never called."""
    callback = MagicMock()
    agent_no_tmux = _make_agent_run(terminal_id=None)
    monitor = _make_monitor_with_db(callback)

    with (
        patch(
            "gobby.agents.tmux.pane_monitor.TmuxSessionManager.list_sessions",
            return_value=[],
        ),
        patch(
            "gobby.storage.agents.LocalAgentRunManager",
        ) as mock_arm_cls,
    ):
        mock_arm_cls.return_value.list_active_for_machine.return_value = [agent_no_tmux]
        await monitor._check_panes()

    callback.assert_not_called()
    assert callback.call_count == 0
    assert not callback.called


@pytest.mark.asyncio
async def test_active_runs_are_paginated_on_worker_thread() -> None:
    callback = MagicMock()
    monitor = _make_monitor_with_db(callback)
    runs = [_make_agent_run(run_id=f"run-{index}", terminal_id=None) for index in range(101)]
    calls: list[tuple[int, int]] = []
    worker_threads: set[int] = set()
    main_thread = threading.get_ident()

    def list_active_for_machine(
        machine_id: str,
        *,
        limit: int,
        offset: int,
    ) -> list[AgentRun]:
        del machine_id
        calls.append((limit, offset))
        worker_threads.add(threading.get_ident())
        return runs[offset : offset + limit]

    with (
        patch(
            "gobby.agents.tmux.pane_monitor.TmuxSessionManager.list_sessions",
            return_value=[],
        ),
        patch("gobby.storage.agents.LocalAgentRunManager") as mock_arm_cls,
        patch.object(monitor, "_check_attention_panes", new_callable=AsyncMock),
    ):
        mock_arm_cls.return_value.list_active_for_machine.side_effect = list_active_for_machine
        await monitor._check_panes()

    assert calls == [(100, 0), (100, 100)]
    assert worker_threads
    assert main_thread not in worker_threads


@pytest.mark.asyncio
async def test_interactive_sessions_use_cursor_pagination_on_worker_thread() -> None:
    callback = MagicMock()
    session_manager = MagicMock()
    session_manager.db = MagicMock()
    monitor = TmuxPaneMonitor(
        detection_registry=DETECTION_REGISTRY,
        session_end_callback=callback,
        session_manager=session_manager,
        registry=runtime_registry(FakeRuntime()),
    )
    sessions = [MagicMock() for _ in range(101)]
    for index, session in enumerate(sessions):
        session.id = f"session-{index}"
        session.updated_at = datetime(2026, 1, 1, 12, index % 60, tzinfo=UTC)
    pages = [sessions[:100], sessions[100:]]
    calls: list[dict[str, object]] = []
    worker_threads: set[int] = set()
    main_thread = threading.get_ident()

    def list_sessions(**kwargs: object) -> list[MagicMock]:
        calls.append(kwargs)
        worker_threads.add(threading.get_ident())
        return pages.pop(0)

    session_manager.list.side_effect = list_sessions

    result = await monitor._list_interactive_sessions()

    assert result == sessions
    assert len(calls) == 2
    assert calls[0]["cursor_updated_at"] is None
    assert calls[0]["cursor_id"] is None
    assert calls[1]["cursor_updated_at"] == sessions[99].updated_at.isoformat()
    assert calls[1]["cursor_id"] == sessions[99].id
    assert worker_threads
    assert main_thread not in worker_threads


@pytest.mark.asyncio
async def test_tmux_list_timeout_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """A transient tmux list timeout should not emit a warning traceback."""
    callback = MagicMock()
    monitor = _make_monitor_with_db(callback)
    caplog.set_level("DEBUG", logger="gobby.agents.tmux.pane_monitor")

    with patch(
        "gobby.agents.tmux.pane_monitor.TmuxSessionManager.list_sessions",
        side_effect=TimeoutError,
    ):
        await monitor._check_panes()

    callback.assert_not_called()
    assert "failed to list tmux sessions" not in caplog.text
    timeout_records = [
        record
        for record in caplog.records
        if record.message == "TmuxPaneMonitor: timed out listing tmux sessions"
    ]
    assert timeout_records
    assert timeout_records[0].timeout_seconds == TMUX_COMMAND_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_all_alive_noop() -> None:
    """When all agent tmux sessions are still alive, callback is never called."""
    callback = MagicMock()
    agent = _make_agent_run(terminal_id="gobby-agent-1")
    monitor = _make_monitor_with_db(callback)

    with (
        patch(
            "gobby.agents.tmux.pane_monitor.TmuxSessionManager.list_sessions",
            return_value=[TmuxSessionInfo(name="gobby-agent-1")],
        ),
        patch(
            "gobby.storage.agents.LocalAgentRunManager",
        ) as mock_arm_cls,
    ):
        mock_arm_cls.return_value.list_active_for_machine.return_value = [agent]
        await monitor._check_panes()

    callback.assert_not_called()
    assert callback.call_count == 0
    assert not callback.called


@pytest.mark.asyncio
async def test_alive_tmux_reused_pid_triggers_callback() -> None:
    callback = MagicMock()
    agent = _make_agent_run(pid=999)
    session_obj = _make_session_obj()
    monitor = _make_monitor_with_db(callback)

    with (
        patch(
            "gobby.agents.tmux.pane_monitor.TmuxSessionManager.list_sessions",
            return_value=[TmuxSessionInfo(name="gobby-agent-1")],
        ),
        patch("gobby.storage.agents.LocalAgentRunManager") as mock_arm_cls,
        patch.object(monitor, "_lookup_session", return_value=session_obj),
        patch(
            "gobby.agents.tmux.pane_monitor.pid_matches_agent_identity",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_identity,
    ):
        mock_arm_cls.return_value.list_active_for_machine.return_value = [agent]
        await monitor._check_panes()

    mock_identity.assert_awaited_once_with(
        999,
        provider="test",
        session_id="sess-1",
        unverifiable_result=True,
    )
    callback.assert_called_once()
    event: HookEvent = callback.call_args[0][0]
    assert event.event_type == HookEventType.SESSION_END
    assert event.session_id == "ext-1"
    assert event.metadata["_platform_session_id"] == "sess-1"
    assert event.metadata["_tmux_pane_death"] is True


@pytest.mark.asyncio
async def test_dead_session_triggers_callback() -> None:
    """When a tmux session is gone, callback is called with correct HookEvent."""
    callback_thread_ids: list[int] = []
    callback = MagicMock(
        side_effect=lambda _event: callback_thread_ids.append(threading.get_ident())
    )
    event_loop_thread_id = threading.get_ident()
    agent = _make_agent_run(child_session_id="sess-dead", terminal_id="gobby-dead")
    session_obj = _make_session_obj(session_id="sess-dead", external_id="ext-dead", source="claude")
    monitor = _make_monitor_with_db(callback)

    with (
        patch(
            "gobby.agents.tmux.pane_monitor.TmuxSessionManager.list_sessions",
            return_value=[TmuxSessionInfo(name="gobby-alive")],
        ),
        patch(
            "gobby.storage.agents.LocalAgentRunManager",
        ) as mock_arm_cls,
        patch.object(monitor, "_lookup_session", return_value=session_obj),
    ):
        mock_arm_cls.return_value.list_active_for_machine.return_value = [agent]
        await monitor._check_panes()

    callback.assert_called_once()
    event: HookEvent = callback.call_args[0][0]
    assert event.event_type == HookEventType.SESSION_END
    assert event.session_id == "ext-dead"
    assert event.metadata["_platform_session_id"] == "sess-dead"
    assert event.metadata["_tmux_pane_death"] is True
    assert len(callback_thread_ids) == 1
    assert callback_thread_ids[0] != event_loop_thread_id


@pytest.mark.asyncio
async def test_recently_ended_prevents_double_fire() -> None:
    """mark_recently_ended blocks re-fire for the same session."""
    callback = MagicMock()
    agent = _make_agent_run(child_session_id="sess-ended", terminal_id="gobby-ended")
    monitor = _make_monitor_with_db(callback)

    # Mark as recently ended
    monitor.mark_recently_ended("sess-ended")

    with (
        patch(
            "gobby.agents.tmux.pane_monitor.TmuxSessionManager.list_sessions",
            return_value=[],
        ),
        patch(
            "gobby.storage.agents.LocalAgentRunManager",
        ) as mock_arm_cls,
    ):
        mock_arm_cls.return_value.list_active_for_machine.return_value = [agent]
        await monitor._check_panes()

    callback.assert_not_called()
    assert callback.call_count == 0
    assert not callback.called


@pytest.mark.asyncio
async def test_recently_ended_expires() -> None:
    """Old entries get pruned; agent triggers callback normally after TTL."""
    callback = MagicMock()
    agent = _make_agent_run(child_session_id="sess-old", terminal_id="gobby-old")
    session_obj = _make_session_obj(session_id="sess-old", external_id="ext-old")
    monitor = _make_monitor_with_db(callback)

    # Insert an entry that's already expired
    monitor._recently_ended["sess-old"] = time.monotonic() - _RECENTLY_ENDED_TTL - 1

    with (
        patch(
            "gobby.agents.tmux.pane_monitor.TmuxSessionManager.list_sessions",
            return_value=[],
        ),
        patch(
            "gobby.storage.agents.LocalAgentRunManager",
        ) as mock_arm_cls,
        patch.object(monitor, "_lookup_session", return_value=session_obj),
    ):
        mock_arm_cls.return_value.list_active_for_machine.return_value = [agent]
        await monitor._check_panes()

    callback.assert_called_once()
    assert callback.call_count == 1
    assert callback.call_args is not None


@pytest.mark.asyncio
async def test_callback_exception_no_crash() -> None:
    """An error in callback doesn't prevent processing other agents."""
    callback = MagicMock(side_effect=RuntimeError("boom"))
    agent = _make_agent_run(child_session_id="sess-err", terminal_id="gobby-err")
    session_obj = _make_session_obj(session_id="sess-err")
    monitor = _make_monitor_with_db(callback)

    with (
        patch(
            "gobby.agents.tmux.pane_monitor.TmuxSessionManager.list_sessions",
            return_value=[],
        ),
        patch(
            "gobby.storage.agents.LocalAgentRunManager",
        ) as mock_arm_cls,
        patch.object(monitor, "_lookup_session", return_value=session_obj),
    ):
        mock_arm_cls.return_value.list_active_for_machine.return_value = [agent]
        # Should not raise
        await monitor._check_panes()

    # Callback was called but raised; session should still be marked recently ended
    assert "sess-err" in monitor._recently_ended
    assert len(monitor._recently_ended) == 1


@pytest.mark.asyncio
async def test_pool_outage_logs_throttled_warning_without_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PoolTimeout from the DB layer logs a throttled WARNING and skips the pass."""
    from psycopg_pool import PoolTimeout

    import gobby.agents.tmux.pane_monitor as pane_monitor_module

    callback = MagicMock()
    monitor = _make_monitor_with_db(callback)
    pane_monitor_module._pool_outage_log._last_logged.clear()

    with (
        patch(
            "gobby.agents.tmux.pane_monitor.TmuxSessionManager.list_sessions",
            return_value=[],
        ),
        patch(
            "gobby.storage.agents.LocalAgentRunManager",
        ) as mock_arm_cls,
        caplog.at_level(logging.DEBUG, logger="gobby.agents.tmux.pane_monitor"),
    ):
        mock_arm_cls.return_value.list_active_for_machine.side_effect = PoolTimeout(
            "couldn't get a connection after 5.00 sec"
        )
        await monitor._check_panes()
        await monitor._check_panes()

    callback.assert_not_called()
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "hub temporarily unavailable" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "hub temporarily unavailable; skipping pass" in warnings[0].getMessage()
    assert warnings[0].exc_info is None
