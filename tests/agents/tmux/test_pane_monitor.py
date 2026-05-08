"""Tests for TmuxPaneMonitor."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.tmux.pane_monitor import _RECENTLY_ENDED_TTL, TmuxPaneMonitor
from gobby.agents.tmux.session_manager import TmuxSessionInfo
from gobby.hooks.events import HookEvent, HookEventType
from gobby.storage.agents import AgentRun

pytestmark = pytest.mark.unit


def _make_agent_run(
    run_id: str = "run-1",
    child_session_id: str = "sess-1",
    parent_session_id: str = "parent-1",
    tmux_session_name: str | None = "gobby-agent-1",
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
        tmux_session_name=tmux_session_name,
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


def _make_monitor_with_db(callback: MagicMock) -> TmuxPaneMonitor:
    """Create a TmuxPaneMonitor with a mock session manager."""
    mock_db = MagicMock()
    mock_session_manager = MagicMock()
    mock_session_manager.db = mock_db
    monitor = TmuxPaneMonitor(
        session_end_callback=callback,
        poll_interval=1.0,
        session_manager=mock_session_manager,
    )
    return monitor


@pytest.mark.asyncio
async def test_no_tmux_agents_noop() -> None:
    """When no agents have tmux_session_name, callback is never called."""
    callback = MagicMock()
    agent_no_tmux = _make_agent_run(tmux_session_name=None)
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
        mock_arm_cls.return_value.list_active.return_value = [agent_no_tmux]
        await monitor._check_panes()

    callback.assert_not_called()
    assert callback.call_count == 0
    assert not callback.called


@pytest.mark.asyncio
async def test_tmux_list_timeout_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """A transient tmux list timeout should not emit a warning traceback."""
    callback = MagicMock()
    monitor = _make_monitor_with_db(callback)
    caplog.set_level("WARNING", logger="gobby.agents.tmux.pane_monitor")

    with patch(
        "gobby.agents.tmux.pane_monitor.TmuxSessionManager.list_sessions",
        side_effect=TimeoutError,
    ):
        await monitor._check_panes()

    callback.assert_not_called()
    assert "failed to list tmux sessions" not in caplog.text


@pytest.mark.asyncio
async def test_all_alive_noop() -> None:
    """When all agent tmux sessions are still alive, callback is never called."""
    callback = MagicMock()
    agent = _make_agent_run(tmux_session_name="gobby-agent-1")
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
        mock_arm_cls.return_value.list_active.return_value = [agent]
        await monitor._check_panes()

    callback.assert_not_called()
    assert callback.call_count == 0
    assert not callback.called


@pytest.mark.asyncio
async def test_dead_session_triggers_callback() -> None:
    """When a tmux session is gone, callback is called with correct HookEvent."""
    callback = MagicMock()
    agent = _make_agent_run(child_session_id="sess-dead", tmux_session_name="gobby-dead")
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
        mock_arm_cls.return_value.list_active.return_value = [agent]
        await monitor._check_panes()

    callback.assert_called_once()
    event: HookEvent = callback.call_args[0][0]
    assert event.event_type == HookEventType.SESSION_END
    assert event.session_id == "ext-dead"
    assert event.metadata["_platform_session_id"] == "sess-dead"
    assert event.metadata["_tmux_pane_death"] is True


@pytest.mark.asyncio
async def test_recently_ended_prevents_double_fire() -> None:
    """mark_recently_ended blocks re-fire for the same session."""
    callback = MagicMock()
    agent = _make_agent_run(child_session_id="sess-ended", tmux_session_name="gobby-ended")
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
        mock_arm_cls.return_value.list_active.return_value = [agent]
        await monitor._check_panes()

    callback.assert_not_called()
    assert callback.call_count == 0
    assert not callback.called


@pytest.mark.asyncio
async def test_recently_ended_expires() -> None:
    """Old entries get pruned; agent triggers callback normally after TTL."""
    callback = MagicMock()
    agent = _make_agent_run(child_session_id="sess-old", tmux_session_name="gobby-old")
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
        mock_arm_cls.return_value.list_active.return_value = [agent]
        await monitor._check_panes()

    callback.assert_called_once()
    assert callback.call_count == 1
    assert callback.call_args is not None


@pytest.mark.asyncio
async def test_callback_exception_no_crash() -> None:
    """An error in callback doesn't prevent processing other agents."""
    callback = MagicMock(side_effect=RuntimeError("boom"))
    agent = _make_agent_run(child_session_id="sess-err", tmux_session_name="gobby-err")
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
        mock_arm_cls.return_value.list_active.return_value = [agent]
        # Should not raise
        await monitor._check_panes()

    # Callback was called but raised; session should still be marked recently ended
    assert "sess-err" in monitor._recently_ended
    assert len(monitor._recently_ended) == 1
