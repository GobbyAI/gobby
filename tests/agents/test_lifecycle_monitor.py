"""Tests for gobby.agents.lifecycle_monitor module.

Tests for the AgentLifecycleMonitor that detects dead tmux sessions
and completed/failed autonomous tasks, and marks their agent DB records.

All tests are DB-driven — no in-memory RunningAgentRegistry.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.database import LocalDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


@pytest.fixture
def agent_run_manager(temp_db: LocalDatabase) -> LocalAgentRunManager:
    return LocalAgentRunManager(temp_db)


@pytest.fixture
def sample_session(
    session_manager: SessionManager,
    sample_project: dict,
) -> dict:
    session = session_manager.register(
        external_id="lifecycle-test-session",
        machine_id="machine-1",
        source="claude",
        project_id=sample_project["id"],
    )
    return session.to_dict()


@pytest.fixture
def monitor(
    agent_run_manager: LocalAgentRunManager,
    temp_db: LocalDatabase,
) -> AgentLifecycleMonitor:
    return AgentLifecycleMonitor(
        agent_run_manager=agent_run_manager,
        db=temp_db,
        check_interval_seconds=1.0,
    )


def _make_terminal_run(
    agent_run_manager: LocalAgentRunManager,
    sample_session: dict,
    run_id: str = "run-abc123",
    tmux_session_name: str = "gobby-1234567890-abc123",
    pid: int | None = None,
    timeout_seconds: float | None = None,
    child_session_id: str | None = None,
    clone_id: str | None = None,
    requested_reasoning_effort: str | None = None,
) -> AgentRun:
    """Helper to create a running terminal-mode agent in the DB."""
    run = agent_run_manager.create(
        parent_session_id=sample_session["id"],
        provider="claude",
        prompt="test",
        run_id=run_id,
        child_session_id=child_session_id,
        timeout_seconds=timeout_seconds,
        requested_reasoning_effort=requested_reasoning_effort,
    )
    agent_run_manager.start(run.id)
    agent_run_manager.update_runtime(
        run.id,
        pid=pid,
        tmux_session_name=tmux_session_name,
        clone_id=clone_id,
    )
    stored_run = agent_run_manager.get(run.id)
    assert stored_run is not None
    return stored_run


def _make_autonomous_run(
    agent_run_manager: LocalAgentRunManager,
    sample_session: dict,
    monitor: AgentLifecycleMonitor,
    run_id: str = "run-auto",
    task: asyncio.Task[Any] | None = None,
    child_session_id: str | None = None,
    clone_id: str | None = None,
) -> AgentRun:
    """Helper to create a running autonomous-mode agent in the DB with optional asyncio.Task."""
    run = agent_run_manager.create(
        parent_session_id=sample_session["id"],
        provider="claude",
        prompt="test",
        run_id=run_id,
        child_session_id=child_session_id,
    )
    agent_run_manager.start(run.id)
    agent_run_manager.update_runtime(
        run.id,
        clone_id=clone_id,
    )
    if task is not None:
        monitor.register_async_task(run.id, task)
    stored_run = agent_run_manager.get(run.id)
    assert stored_run is not None
    return stored_run


class TestCheckDeadAgents:
    """Tests for check_unhealthy_agents."""

    @pytest.mark.asyncio
    async def test_detects_dead_tmux_session(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Dead tmux session is detected and agent run marked as failed."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-dead",
            tmux_session_name="gobby-dead",
        )

        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 1

        updated = agent_run_manager.get("run-dead")
        assert updated is not None
        assert updated.status == "error"
        assert "tmux session died" in (updated.error or "")

    @pytest.mark.asyncio
    async def test_skips_alive_tmux_session(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Alive tmux session is left untouched."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-alive",
            tmux_session_name="gobby-alive",
        )

        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=True):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 0

        updated = agent_run_manager.get("run-alive")
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_no_tmux_agents_returns_zero(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """Returns 0 when no terminal agents exist."""
        cleaned = await monitor.check_unhealthy_agents()
        assert cleaned == 0

    @pytest.mark.asyncio
    async def test_skips_already_completed_db_record(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Already-completed DB records are not returned by list_active and not cleaned."""
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-done",
        )
        agent_run_manager.start(run.id)
        agent_run_manager.complete(run.id, result="done")

        cleaned = await monitor.check_unhealthy_agents()

        # list_active() won't return completed runs, so nothing to clean
        assert cleaned == 0
        # DB status should remain 'success'
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "success"

    @pytest.mark.asyncio
    async def test_handles_tmux_check_error(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Errors checking tmux are caught per-agent, don't crash the loop."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-err",
            tmux_session_name="gobby-err",
        )

        with patch.object(
            monitor._tmux,
            "has_session",
            new_callable=AsyncMock,
            side_effect=OSError("tmux socket gone"),
        ):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 0
        # Agent stays running since we couldn't determine its status
        updated = agent_run_manager.get("run-err")
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_releases_worktrees_on_dead_agent(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: LocalDatabase,
        session_manager: SessionManager,
    ) -> None:
        """Worktrees are released when a dead agent is cleaned up."""
        child_session = session_manager.register(
            external_id="child-sess-wt",
            machine_id="machine-1",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        mock_coordinator = MagicMock()
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_coordinator=mock_coordinator,
        )

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-wt",
            tmux_session_name="gobby-wt",
            child_session_id=child_session.id,
        )

        with patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            await mon.check_unhealthy_agents()

        mock_coordinator.release_session_worktrees.assert_called_once_with(child_session.id)
        assert mock_coordinator.release_session_worktrees.call_count == 1
        assert mock_coordinator.release_session_worktrees.call_args is not None


class TestStartStop:
    """Tests for monitor start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_task(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """start() creates a background asyncio task."""
        await monitor.start()
        try:
            assert monitor._task is not None
            assert not monitor._task.done()
        finally:
            await monitor.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """stop() cancels the background task."""
        await monitor.start()
        await monitor.stop()
        assert monitor._task is None

    @pytest.mark.asyncio
    async def test_double_start_is_noop(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """Calling start() twice doesn't create duplicate tasks."""
        await monitor.start()
        task1 = monitor._task
        await monitor.start()
        task2 = monitor._task
        assert task1 is task2
        await monitor.stop()


class TestCheckIdleAgents:
    """Tests for idle agent detection and reprompting."""

    @pytest.fixture
    def idle_monitor(
        self,
        agent_run_manager: LocalAgentRunManager,
        temp_db: LocalDatabase,
    ) -> AgentLifecycleMonitor:
        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        return AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            check_interval_seconds=1.0,
            tmux_config=config,
        )

    @pytest.mark.asyncio
    async def test_active_agent_not_touched(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Active agents should not be reprompted."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-active",
            tmux_session_name="gobby-active",
        )

        with patch.object(
            idle_monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            return_value="Running tests...\n",
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 0

    @pytest.mark.asyncio
    async def test_idle_agent_reprompted(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Idle agent past timeout should be reprompted."""
        import time

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-idle",
            tmux_session_name="gobby-idle",
        )

        # Pre-set idle state to simulate timeout elapsed
        state = idle_monitor._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 120

        with (
            patch.object(
                idle_monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="\u276f\n"
            ),
            patch.object(
                idle_monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 1
        mock_send.assert_called_once()
        assert "Continue working" in mock_send.call_args[0][1]

    @pytest.mark.asyncio
    async def test_idle_agent_failed_after_max_reprompts(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Agent should be failed after exhausting reprompt attempts."""
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-exhausted",
            tmux_session_name="gobby-exhausted",
        )

        # Set reprompt count at max
        state = idle_monitor._idle_detector.get_state(run.id)
        state.reprompt_count = 2  # max_reprompt_attempts = 2

        with (
            patch.object(
                idle_monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="\u276f\n"
            ),
            patch.object(
                idle_monitor._tmux, "kill_session", new_callable=AsyncMock, return_value=True
            ),
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 1
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"
        assert "idle" in (updated.error or "").lower()

    @pytest.mark.asyncio
    async def test_context_full_fails_immediately(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Context-full agent should be failed immediately without reprompt."""
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-ctx-full",
            tmux_session_name="gobby-ctx",
        )

        with (
            patch.object(
                idle_monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="The context window is full.\n\u276f\n",
            ),
            patch.object(
                idle_monitor._tmux, "kill_session", new_callable=AsyncMock, return_value=True
            ),
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 1
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"
        assert "context" in (updated.error or "").lower()

    @pytest.mark.asyncio
    async def test_disabled_idle_check(
        self,
        agent_run_manager: LocalAgentRunManager,
        temp_db: LocalDatabase,
        sample_session: dict,
    ) -> None:
        """Idle check should be skipped when disabled."""
        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(idle_check_enabled=False)
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            tmux_config=config,
        )
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-skip",
            tmux_session_name="gobby-skip",
        )

        handled = await mon.check_idle_agents()
        assert handled == 0

    @pytest.mark.asyncio
    async def test_capture_pane_failure_skipped(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Agent should be skipped if capture_pane returns None."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-no-capture",
            tmux_session_name="gobby-nocap",
        )

        with patch.object(
            idle_monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value=None
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 0

    @pytest.mark.asyncio
    async def test_recent_session_activity_skips_pane_check(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: LocalDatabase,
    ) -> None:
        """Agent with recent session updated_at should be considered active,
        skipping pane pattern matching entirely."""
        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )

        # Create a child session and register it
        child = session_manager.register(
            external_id="child-session",
            machine_id="machine-1",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        # Touch it so updated_at is very recent
        session_manager.touch(child.id)

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-session-active",
            tmux_session_name="gobby-session-active",
            child_session_id=child.id,
        )

        with patch.object(mon._tmux, "capture_pane", new_callable=AsyncMock) as mock_capture:
            handled = await mon.check_idle_agents()

        assert handled == 0
        # Pane capture should NOT have been called — session activity was sufficient
        mock_capture.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_session_falls_through_to_pane_check(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: LocalDatabase,
    ) -> None:
        """Agent with stale session updated_at should fall through to pane detection."""
        import time
        from datetime import UTC, datetime, timedelta

        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )

        # Create child session with stale updated_at
        child = session_manager.register(
            external_id="child-stale",
            machine_id="machine-1",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        # Backdate updated_at to make it stale
        stale_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (stale_time, child.id),
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-session-stale",
            tmux_session_name="gobby-session-stale",
            child_session_id=child.id,
        )

        # Pre-set idle state to simulate timeout elapsed
        state = mon._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 120

        with (
            patch.object(
                mon._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"
            ) as mock_capture,
            patch.object(
                mon._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            handled = await mon.check_idle_agents()

        assert handled == 1
        # Pane capture SHOULD have been called since session was stale
        mock_capture.assert_called_once()
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_naive_legacy_session_timestamp_is_treated_as_utc(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: LocalDatabase,
    ) -> None:
        """Naive legacy updated_at values should not crash idle checks."""
        import time
        from datetime import UTC, datetime, timedelta

        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )
        child = session_manager.register(
            external_id="child-naive-stale",
            machine_id="machine-1",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        stale_time = (datetime.now(UTC) - timedelta(seconds=120)).replace(tzinfo=None).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (stale_time, child.id),
        )
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-session-naive-stale",
            tmux_session_name="gobby-session-naive-stale",
            child_session_id=child.id,
        )
        mon._idle_detector.get_state(run.id).first_idle_at = time.monotonic() - 120

        with (
            patch.object(
                mon._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"
            ) as mock_capture,
            patch.object(
                mon._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            handled = await mon.check_idle_agents()

        assert handled == 1
        mock_capture.assert_called_once()
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_xhigh_session_within_scaled_timeout_skips_pane_check(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: LocalDatabase,
    ) -> None:
        """xhigh runs should stay active within the extended idle window."""
        import time
        from datetime import UTC, datetime, timedelta

        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )

        child = session_manager.register(
            external_id="child-xhigh-scaled-active",
            machine_id="machine-1",
            source="codex",
            project_id=sample_session.get("project_id"),
        )
        stale_for_base_timeout = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (stale_for_base_timeout, child.id),
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-xhigh-scaled-active",
            tmux_session_name="gobby-xhigh-scaled-active",
            child_session_id=child.id,
            requested_reasoning_effort=" XHIGH ",
        )

        state = mon._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 120
        state.reprompt_count = 2

        with (
            patch.object(
                mon._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"
            ) as mock_capture,
            patch.object(mon._tmux, "send_keys", new_callable=AsyncMock) as mock_send,
            patch.object(mon._tmux, "kill_session", new_callable=AsyncMock) as mock_kill,
        ):
            handled = await mon.check_idle_agents()

        assert handled == 0
        mock_capture.assert_not_called()
        mock_send.assert_not_called()
        mock_kill.assert_not_called()
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_non_xhigh_session_past_base_timeout_uses_stale_idle_handling(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: LocalDatabase,
    ) -> None:
        """Non-xhigh runs keep the base idle timeout."""
        import time
        from datetime import UTC, datetime, timedelta

        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )

        child = session_manager.register(
            external_id="child-high-base-stale",
            machine_id="machine-1",
            source="codex",
            project_id=sample_session.get("project_id"),
        )
        stale_for_base_timeout = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (stale_for_base_timeout, child.id),
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-high-base-stale",
            tmux_session_name="gobby-high-base-stale",
            child_session_id=child.id,
            requested_reasoning_effort="high",
        )

        state = mon._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 120

        with (
            patch.object(
                mon._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"
            ) as mock_capture,
            patch.object(
                mon._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            handled = await mon.check_idle_agents()

        assert handled == 1
        mock_capture.assert_called_once()
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_xhigh_session_past_scaled_timeout_can_fail_after_reprompts(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: LocalDatabase,
    ) -> None:
        """xhigh runs still fail normally once the extended window expires."""
        from datetime import UTC, datetime, timedelta

        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )

        child = session_manager.register(
            external_id="child-xhigh-scaled-stale",
            machine_id="machine-1",
            source="codex",
            project_id=sample_session.get("project_id"),
        )
        stale_for_scaled_timeout = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (stale_for_scaled_timeout, child.id),
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-xhigh-scaled-stale",
            tmux_session_name="gobby-xhigh-scaled-stale",
            child_session_id=child.id,
            requested_reasoning_effort="xhigh",
        )

        state = mon._idle_detector.get_state(run.id)
        state.reprompt_count = 2

        with (
            patch.object(mon._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
            patch.object(mon._tmux, "kill_session", new_callable=AsyncMock, return_value=True),
        ):
            handled = await mon.check_idle_agents()

        assert handled == 1
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"
        assert "idle" in (updated.error or "").lower()

    @pytest.mark.asyncio
    async def test_stale_session_overrides_active_pane(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: LocalDatabase,
    ) -> None:
        """Stale session should be treated as idle even when pane looks active."""
        import time
        from datetime import UTC, datetime, timedelta

        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )

        # Create child session with stale updated_at
        child = session_manager.register(
            external_id="child-stale-active",
            machine_id="machine-1",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        stale_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (stale_time, child.id),
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-stale-active-pane",
            tmux_session_name="gobby-stale-active",
            child_session_id=child.id,
        )

        # Pre-set idle state to simulate timeout elapsed
        state = mon._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 120

        with (
            patch.object(
                mon._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                # Pane shows active-looking output (running command)
                return_value="Running tests...\nProcessing file 42/100\n",
            ),
            patch.object(
                mon._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            handled = await mon.check_idle_agents()

        # Agent should be reprompted despite active-looking pane
        assert handled == 1
        mock_send.assert_called_once()
        assert "Continue working" in mock_send.call_args[0][1]


class TestCheckTrustPrompts:
    """Tests for trust prompt detection and auto-dismissal."""

    @pytest.mark.asyncio
    async def test_sends_dismiss_key_on_trust_prompt(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Trust prompt detected -> sends Enter to dismiss."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-trust",
            tmux_session_name="gobby-trust",
        )

        trust_output = (
            "Do you trust the files in this folder?\n"
            "1. Trust Folder\n"
            "2. Trust parent Folder\n"
            "3. Don't Trust\n"
        )

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value=trust_output,
            ),
            patch.object(
                monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            handled = await monitor.check_trust_prompts()

        assert handled == 1
        mock_send.assert_called_once_with("gobby-trust", "\n")

    @pytest.mark.asyncio
    async def test_no_action_on_normal_output(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Normal agent output does not trigger trust dismissal."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-normal",
            tmux_session_name="gobby-normal",
        )

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="Running tests...\n",
            ),
            patch.object(monitor._tmux, "send_keys", new_callable=AsyncMock) as mock_send,
        ):
            handled = await monitor.check_trust_prompts()

        assert handled == 0
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_dismiss_twice(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """After dismissal, the same agent is not dismissed again."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-once",
            tmux_session_name="gobby-once",
        )

        trust_output = "Do you trust the files in this folder?\n"

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value=trust_output,
            ),
            patch.object(
                monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            # First call should dismiss
            handled1 = await monitor.check_trust_prompts()
            # Second call should skip (already dismissed)
            handled2 = await monitor.check_trust_prompts()

        assert handled1 == 1
        assert handled2 == 0
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_non_terminal_agents(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Non-terminal agents are not checked for trust prompts."""
        _make_autonomous_run(
            agent_run_manager,
            sample_session,
            monitor,
            run_id="run-auto-trust",
        )

        handled = await monitor.check_trust_prompts()
        assert handled == 0

    @pytest.mark.asyncio
    async def test_skips_when_capture_pane_fails(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Agent is skipped if capture_pane returns None."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-nocap",
            tmux_session_name="gobby-nocap",
        )

        with patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value=None):
            handled = await monitor.check_trust_prompts()

        assert handled == 0

    @pytest.mark.asyncio
    async def test_cleared_on_dead_agent_cleanup(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Prompt detector state is cleared when a dead agent is cleaned up."""
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-cleanup",
            tmux_session_name="gobby-cleanup",
        )

        # Pre-mark as dismissed
        monitor._prompt_detector.mark_dismissed(run.id)

        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            await monitor.check_unhealthy_agents()

        # State should be cleared after cleanup
        assert monitor._prompt_detector.was_dismissed(run.id) is False


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------


class TestCheckExpiredAgents:
    """Tests for timeout-based expiration in check_unhealthy_agents."""

    @pytest.mark.asyncio
    async def test_no_agents_returns_zero(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """Returns 0 when no agents exist."""
        cleaned = await monitor.check_unhealthy_agents()
        assert cleaned == 0

    @pytest.mark.asyncio
    async def test_agent_without_timeout_skipped(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Agents without timeout set are not killed by timeout check."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-no-timeout",
            tmux_session_name="gobby-no-timeout",
            timeout_seconds=None,
        )
        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=True):
            cleaned = await monitor.check_unhealthy_agents()
        assert cleaned == 0

    @pytest.mark.asyncio
    async def test_agent_within_timeout_skipped(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Agents within their timeout are not killed."""
        # Agent just started, timeout is 1 hour — should not be expired
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-not-expired",
            tmux_session_name="gobby-not-expired",
            timeout_seconds=3600,
        )
        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=True):
            cleaned = await monitor.check_unhealthy_agents()
        assert cleaned == 0

    @pytest.mark.asyncio
    async def test_expired_agent_killed(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: LocalDatabase,
    ) -> None:
        """Expired agent is killed and marked as timed out."""
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-expired",
            timeout_seconds=300,
        )
        agent_run_manager.start(run.id)
        agent_run_manager.update_runtime(
            run.id,
            tmux_session_name="gobby-expired",
        )
        # Backdate started_at to simulate expiration
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=600)).isoformat()
        temp_db.execute(
            "UPDATE agent_runs SET started_at = ? WHERE id = ?",
            (past, run.id),
        )

        with (
            patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=True),
            patch("gobby.agents.lifecycle_monitor.kill_agent", new_callable=AsyncMock),
        ):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 1
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "timeout"
        assert "timeout" in (updated.error or "").lower()

    @pytest.mark.asyncio
    async def test_expired_agent_expires_child_session(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: LocalDatabase,
        session_manager: SessionManager,
    ) -> None:
        """Timed-out agent runs expire their child session."""
        child_session = session_manager.register(
            external_id="child-sess-timeout",
            machine_id="machine-1",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-expire-child",
            timeout_seconds=300,
            child_session_id=child_session.id,
        )
        agent_run_manager.start(run.id)
        agent_run_manager.update_runtime(
            run.id,
            tmux_session_name="gobby-expire-child",
        )
        past = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
        temp_db.execute(
            "UPDATE agent_runs SET started_at = ? WHERE id = ?",
            (past, run.id),
        )

        with (
            patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=True),
            patch("gobby.agents.lifecycle_monitor.kill_agent", new_callable=AsyncMock),
        ):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 1
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "timeout"
        assert session_manager.get(child_session.id).status == "expired"

    @pytest.mark.asyncio
    async def test_terminal_completed_run_expires_child_session(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: LocalDatabase,
        session_manager: SessionManager,
    ) -> None:
        """Already-terminal agent runs expire sessions even if their panes remain alive."""
        child_session = session_manager.register(
            external_id="child-sess-completed-run",
            machine_id="machine-1",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-terminal-completed",
            child_session_id=child_session.id,
        )
        completed_at = datetime.now(UTC).isoformat()
        temp_db.execute(
            """
            UPDATE agent_runs
            SET status = 'success', completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (completed_at, completed_at, run.id),
        )

        expired = await monitor.expire_terminal_run_sessions()

        assert expired == 1
        assert session_manager.get(child_session.id).status == "expired"

    @pytest.mark.asyncio
    async def test_expired_agent_releases_worktrees(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: LocalDatabase,
        session_manager: SessionManager,
    ) -> None:
        """Expired agent cleanup releases worktrees."""
        child_session = session_manager.register(
            external_id="child-sess-exp-wt",
            machine_id="machine-1",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        mock_coordinator = MagicMock()
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_coordinator=mock_coordinator,
        )

        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-exp-wt",
            timeout_seconds=300,
            child_session_id=child_session.id,
        )
        agent_run_manager.start(run.id)
        agent_run_manager.update_runtime(
            run.id,
            tmux_session_name="gobby-exp-wt",
        )
        # Backdate started_at
        past = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
        temp_db.execute(
            "UPDATE agent_runs SET started_at = ? WHERE id = ?",
            (past, run.id),
        )

        with (
            patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=True),
            patch("gobby.agents.lifecycle_monitor.kill_agent", new_callable=AsyncMock),
        ):
            await mon.check_unhealthy_agents()

        mock_coordinator.release_session_worktrees.assert_called_once_with(child_session.id)
        assert mock_coordinator.release_session_worktrees.call_count == 1
        assert mock_coordinator.release_session_worktrees.call_args is not None

    @pytest.mark.asyncio
    async def test_expired_agent_releases_clones(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: LocalDatabase,
    ) -> None:
        """Expired agent cleanup releases clones."""
        mock_clone_storage = MagicMock()
        mock_clone_storage.release = MagicMock()
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            clone_storage=mock_clone_storage,
        )

        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-exp-cl",
            timeout_seconds=300,
        )
        agent_run_manager.start(run.id)
        agent_run_manager.update_runtime(
            run.id,
            tmux_session_name="gobby-exp-cl",
            clone_id="clone-456",
        )
        # Backdate started_at
        past = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
        temp_db.execute(
            "UPDATE agent_runs SET started_at = ? WHERE id = ?",
            (past, run.id),
        )

        with (
            patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=True),
            patch("gobby.agents.lifecycle_monitor.kill_agent", new_callable=AsyncMock),
        ):
            await mon.check_unhealthy_agents()

        mock_clone_storage.release.assert_called_once_with("clone-456")
        assert mock_clone_storage.release.call_count == 1
        assert mock_clone_storage.release.call_args is not None


class TestCheckProviderStalls:
    """Tests for check_provider_stalls."""

    @pytest.mark.asyncio
    async def test_no_agents_returns_zero(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """Returns 0 when no agents exist."""
        stalled = await monitor.check_provider_stalls()
        assert stalled == 0

    @pytest.mark.asyncio
    async def test_healthy_agent_not_counted(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Healthy agent is not counted as stalled."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-healthy",
            tmux_session_name="gobby-healthy",
        )

        with patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            return_value="Working on task...\n",
        ):
            stalled = await monitor.check_provider_stalls()

        assert stalled == 0

    @pytest.mark.asyncio
    async def test_capture_pane_error_handled(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Error during capture_pane is handled gracefully."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-stall-err",
            tmux_session_name="gobby-stall-err",
        )

        with patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            side_effect=OSError("tmux error"),
        ):
            stalled = await monitor.check_provider_stalls()

        assert stalled == 0


class TestCheckProviderStallsKillsAgent:
    """Tests that check_provider_stalls kills agents on confirmed stall."""

    @pytest.mark.asyncio
    async def test_kills_agent_on_confirmed_stall(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Confirmed PROVIDER_STALL kills the agent and marks it failed."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-stall-kill",
            tmux_session_name="gobby-stall-kill",
        )

        rate_limit_output = "Error: 429 Too Many Requests - rate limit exceeded\n"

        call_count = 0

        async def capture_pane_side_effect(session_name: str, lines: int = 30) -> str:
            nonlocal call_count
            call_count += 1
            return rate_limit_output

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                side_effect=capture_pane_side_effect,
            ),
            patch.object(
                monitor._tmux,
                "has_session",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                monitor._tmux,
                "kill_session",
                new_callable=AsyncMock,
            ) as mock_kill,
        ):
            # First check: sets consecutive_hits=1, returns UNKNOWN
            stalled = await monitor.check_provider_stalls()
            assert stalled == 0

            # Advance stall classifier's internal clock past min interval
            import time

            state = monitor._stall_classifier._states.get("run-stall-kill")
            assert state is not None
            state.last_check_at = time.monotonic() - 35

            # Second check: consecutive_hits=2, confirms PROVIDER_STALL → kill
            stalled = await monitor.check_provider_stalls()
            assert stalled == 1
            mock_kill.assert_called_once_with("gobby-stall-kill")

        updated = agent_run_manager.get("run-stall-kill")
        assert updated is not None
        assert updated.status == "error"
        assert "Provider stall" in (updated.error or "")
        assert "rate limit" in (updated.error or "").lower()

    @pytest.mark.asyncio
    async def test_stall_error_matches_provider_pattern(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Error message from stall kill matches StallClassifier.is_provider_error."""
        from gobby.agents.stall_classifier import StallClassifier

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-stall-pattern",
            tmux_session_name="gobby-stall-pattern",
        )

        import time

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="Error: 503 Service Unavailable overloaded\n",
            ),
            patch.object(
                monitor._tmux,
                "has_session",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                monitor._tmux,
                "kill_session",
                new_callable=AsyncMock,
            ),
        ):
            await monitor.check_provider_stalls()
            state = monitor._stall_classifier._states.get("run-stall-pattern")
            assert state is not None
            state.last_check_at = time.monotonic() - 35
            await monitor.check_provider_stalls()

        updated = agent_run_manager.get("run-stall-pattern")
        assert updated is not None
        classifier = StallClassifier()
        assert classifier.is_provider_error(updated.error)


class TestCheckInitializationTimeout:
    """Tests for check_initialization_timeout."""

    @pytest.mark.asyncio
    async def test_kills_uninitialized_agent(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Agent that never initialized is killed after init_timeout_seconds."""
        # Create a child session with updated_at == created_at
        child = session_manager.register(
            external_id="child-uninit",
            machine_id="machine-1",
            source="gemini",
            project_id=sample_project["id"],
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-uninit",
            tmux_session_name="gobby-uninit",
            child_session_id=child.id,
        )

        # Backdate started_at to exceed init_timeout
        backdated = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
        agent_run_manager.db.execute(
            "UPDATE agent_runs SET started_at = ? WHERE id = ?",
            (backdated, run.id),
        )

        monitor._session_manager = session_manager

        with patch.object(
            monitor._tmux,
            "kill_session",
            new_callable=AsyncMock,
        ) as mock_kill:
            killed = await monitor.check_initialization_timeout()

        assert killed == 1
        mock_kill.assert_called_once_with("gobby-uninit")

        updated = agent_run_manager.get("run-uninit")
        assert updated is not None
        assert updated.status == "error"
        assert "connection timed out" in (updated.error or "").lower()
        assert "never initialized" in (updated.error or "").lower()

    @pytest.mark.asyncio
    async def test_skips_initialized_agent(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Agent whose session was updated is NOT killed."""
        child = session_manager.register(
            external_id="child-init",
            machine_id="machine-1",
            source="gemini",
            project_id=sample_project["id"],
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-init",
            tmux_session_name="gobby-init",
            child_session_id=child.id,
        )

        # Backdate started_at
        backdated = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
        agent_run_manager.db.execute(
            "UPDATE agent_runs SET started_at = ? WHERE id = ?",
            (backdated, run.id),
        )

        # Simulate agent activity: backdate created_at so the touch() delta > 5s
        old_created = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        session_manager.db.execute(
            "UPDATE sessions SET created_at = ? WHERE id = ?",
            (old_created, child.id),
        )
        session_manager.touch(child.id)

        monitor._session_manager = session_manager

        with patch.object(
            monitor._tmux,
            "kill_session",
            new_callable=AsyncMock,
        ) as mock_kill:
            killed = await monitor.check_initialization_timeout()

        assert killed == 0
        mock_kill.assert_not_called()

        updated = agent_run_manager.get("run-init")
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_skips_young_agent(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Agent under init_timeout_seconds is NOT killed even if uninitialized."""
        child = session_manager.register(
            external_id="child-young",
            machine_id="machine-1",
            source="gemini",
            project_id=sample_project["id"],
        )

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-young",
            tmux_session_name="gobby-young",
            child_session_id=child.id,
        )
        # started_at is "now" by default — well under 120s

        monitor._session_manager = session_manager

        with patch.object(
            monitor._tmux,
            "kill_session",
            new_callable=AsyncMock,
        ) as mock_kill:
            killed = await monitor.check_initialization_timeout()

        assert killed == 0
        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_naive_legacy_init_timestamps_are_treated_as_utc(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Naive started_at/created_at/updated_at values should not crash init checks."""
        child = session_manager.register(
            external_id="child-naive-uninit",
            machine_id="machine-1",
            source="gemini",
            project_id=sample_project["id"],
        )
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-naive-uninit",
            tmux_session_name="gobby-naive-uninit",
            child_session_id=child.id,
        )
        started = (datetime.now(UTC) - timedelta(seconds=200)).replace(tzinfo=None).isoformat()
        session_time = (datetime.now(UTC) - timedelta(seconds=200)).replace(tzinfo=None).isoformat()
        agent_run_manager.db.execute(
            "UPDATE agent_runs SET started_at = ? WHERE id = ?",
            (started, run.id),
        )
        session_manager.db.execute(
            "UPDATE sessions SET created_at = ?, updated_at = ? WHERE id = ?",
            (session_time, session_time, child.id),
        )
        monitor._session_manager = session_manager

        with patch.object(
            monitor._tmux,
            "kill_session",
            new_callable=AsyncMock,
        ) as mock_kill:
            killed = await monitor.check_initialization_timeout()

        assert killed == 1
        mock_kill.assert_called_once_with("gobby-naive-uninit")

    @pytest.mark.asyncio
    async def test_error_matches_provider_pattern(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Error message from init timeout matches StallClassifier.is_provider_error."""
        from gobby.agents.stall_classifier import StallClassifier

        child = session_manager.register(
            external_id="child-pattern",
            machine_id="machine-1",
            source="gemini",
            project_id=sample_project["id"],
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-pattern",
            tmux_session_name="gobby-pattern",
            child_session_id=child.id,
        )

        backdated = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
        agent_run_manager.db.execute(
            "UPDATE agent_runs SET started_at = ? WHERE id = ?",
            (backdated, run.id),
        )

        monitor._session_manager = session_manager

        with patch.object(
            monitor._tmux,
            "kill_session",
            new_callable=AsyncMock,
        ):
            await monitor.check_initialization_timeout()

        updated = agent_run_manager.get("run-pattern")
        assert updated is not None
        classifier = StallClassifier()
        assert classifier.is_provider_error(updated.error), (
            f"Error '{updated.error}' should match provider error patterns"
        )

    @pytest.mark.asyncio
    async def test_no_session_manager_skips(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Without session_manager, check is a no-op."""
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-nosm",
            tmux_session_name="gobby-nosm",
        )

        backdated = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
        agent_run_manager.db.execute(
            "UPDATE agent_runs SET started_at = ? WHERE id = ?",
            (backdated, run.id),
        )

        monitor._session_manager = None
        killed = await monitor.check_initialization_timeout()
        assert killed == 0


class TestCheckLoopPrompts:
    """Tests for loop prompt detection and auto-dismissal."""

    @pytest.mark.asyncio
    async def test_dismisses_loop_prompt(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Loop prompt is dismissed by sending keys."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-loop",
            tmux_session_name="gobby-loop",
        )

        loop_output = "It looks like you may be stuck in a loop. Continue? (y/n)\n"

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value=loop_output,
            ),
            patch.object(
                monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
            patch.object(
                monitor._prompt_detector,
                "detect_loop_prompt",
                return_value=True,
            ),
        ):
            handled = await monitor.check_loop_prompts()

        assert handled == 1
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_loop_prompt(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Normal output does not trigger loop prompt dismissal."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-noloop",
            tmux_session_name="gobby-noloop",
        )

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="Working...\n",
            ),
            patch.object(monitor._tmux, "send_keys", new_callable=AsyncMock) as mock_send,
        ):
            handled = await monitor.check_loop_prompts()

        assert handled == 0
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_non_terminal_agents(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Non-terminal agents are skipped for loop prompt check."""
        _make_autonomous_run(
            agent_run_manager,
            sample_session,
            monitor,
            run_id="run-auto-loop",
        )
        handled = await monitor.check_loop_prompts()
        assert handled == 0

    @pytest.mark.asyncio
    async def test_error_during_loop_check(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Error during loop prompt check is handled gracefully."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-loop-err",
            tmux_session_name="gobby-loop-err",
        )

        with patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            side_effect=OSError("tmux gone"),
        ):
            handled = await monitor.check_loop_prompts()

        assert handled == 0


class TestRecoverTaskFromFailedAgent:
    """Tests for _recover_task_from_failed_agent."""

    @pytest.mark.asyncio
    async def test_no_task_manager_is_noop(
        self,
        agent_run_manager: LocalAgentRunManager,
        temp_db: LocalDatabase,
    ) -> None:
        """Without task_manager, recovery does nothing."""
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            task_manager=None,
        )
        result = await mon._recover_task_from_failed_agent("nonexistent-run")
        assert result is None
        assert mon._task_manager is None

    @pytest.mark.asyncio
    async def test_no_db_run_is_noop(
        self,
        agent_run_manager: LocalAgentRunManager,
        temp_db: LocalDatabase,
    ) -> None:
        """When DB run not found, recovery does nothing."""
        mock_task_manager = MagicMock()
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            task_manager=mock_task_manager,
        )
        await mon._recover_task_from_failed_agent("nonexistent-run")
        mock_task_manager.update_task.assert_not_called()
        assert mock_task_manager.update_task.call_count == 0
        assert not mock_task_manager.update_task.called


class TestSetSessionCoordinator:
    """Tests for set_session_coordinator."""

    def test_sets_coordinator(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """set_session_coordinator updates the coordinator reference."""
        mock_coordinator = MagicMock()
        monitor.set_session_coordinator(mock_coordinator)
        assert monitor._session_coordinator is mock_coordinator


class TestCleanupStalePendingRuns:
    """Tests for cleanup_stale_pending_runs."""

    @pytest.mark.asyncio
    async def test_delegates_to_agent_run_manager(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """cleanup_stale_pending_runs delegates to agent_run_manager."""
        with patch.object(
            monitor._agent_run_manager,
            "cleanup_stale_pending_runs",
            return_value=3,
        ):
            result = await monitor.cleanup_stale_pending_runs()
        assert result == 3


class TestDeadAgentCompletionEvent:
    """Tests for completion event firing in check_unhealthy_agents."""

    @pytest.mark.asyncio
    async def test_fires_completion_on_dead_tmux_agent(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: LocalDatabase,
    ) -> None:
        """Completion event is fired when a dead tmux agent is cleaned up."""
        mock_cr = MagicMock()
        mock_cr.notify = AsyncMock()
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            completion_registry=mock_cr,
        )

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-dead-cr",
            tmux_session_name="gobby-dead-cr",
        )

        with patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            await mon.check_unhealthy_agents()

        mock_cr.notify.assert_called_once()
        assert mock_cr.notify.call_count == 1
        assert mock_cr.notify.call_args is not None

    @pytest.mark.asyncio
    async def test_releases_clones_on_dead_tmux_agent(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: LocalDatabase,
    ) -> None:
        """Clones are released when a dead tmux agent with clone_id is cleaned up."""
        mock_clone_storage = MagicMock()
        mock_clone_storage.release = MagicMock()
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            clone_storage=mock_clone_storage,
        )

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-dead-clone",
            tmux_session_name="gobby-dead-clone",
            clone_id="clone-789",
        )

        with patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            await mon.check_unhealthy_agents()

        mock_clone_storage.release.assert_called_once_with("clone-789")
        assert mock_clone_storage.release.call_count == 1
        assert mock_clone_storage.release.call_args is not None


class TestDeadAgentKillsOrphanedProcess:
    """Tests for killing orphaned processes in check_unhealthy_agents."""

    @pytest.mark.asyncio
    async def test_kills_orphaned_process(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """Orphaned process receives cleanup when tmux is dead."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-orphan-pid",
            tmux_session_name="gobby-orphan-pid",
            pid=999999,  # Non-existent PID
        )

        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 1


class TestSessionExpirationOnCleanup:
    """Tests for session expiration during agent cleanup."""

    @pytest.mark.asyncio
    async def test_session_expired_on_dead_agent(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: LocalDatabase,
        session_manager: SessionManager,
    ) -> None:
        """Session is expired when a dead agent is cleaned up."""
        # Create a child session for the agent
        child_session = session_manager.register(
            external_id="child-session-for-agent",
            machine_id="machine-1",
            source="claude",
            project_id=sample_session.get("project_id"),
        )

        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
        )

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-expire-sess",
            tmux_session_name="gobby-expire-sess",
            child_session_id=child_session.id,
        )

        with patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            cleaned = await mon.check_unhealthy_agents()

        assert cleaned == 1

        # Verify session was expired
        updated_session = session_manager.get(child_session.id)
        assert updated_session is not None
        assert updated_session.status == "expired"

    @pytest.mark.asyncio
    async def test_no_session_manager_skips_expiration(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: LocalDatabase,
    ) -> None:
        """Without session_manager, cleanup still succeeds but skips expiration."""
        mon = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=None,
        )

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-no-sm",
            tmux_session_name="gobby-no-sm",
        )

        with patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            cleaned = await mon.check_unhealthy_agents()

        assert cleaned == 1
        updated = agent_run_manager.get("run-no-sm")
        assert updated is not None
        assert updated.status == "error"


class TestCleanupAgentFdClose:
    """Tests that _cleanup_agent closes registered master fds."""

    @pytest.mark.asyncio
    async def test_cleanup_agent_closes_master_fd(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Registered master_fd is os.close()'d during cleanup."""
        r_fd, w_fd = os.pipe()
        try:
            run = _make_terminal_run(
                agent_run_manager,
                sample_session,
                run_id="run-fd-test",
                tmux_session_name="gobby-fd-test",
            )
            monitor.register_master_fd("run-fd-test", r_fd)

            await monitor._cleanup_agent(run, terminal_payload="test cleanup", is_success=True)

            # fd should be closed — closing again should raise
            with pytest.raises(OSError):
                os.close(r_fd)
            r_fd = -1  # mark as already closed
        finally:
            if r_fd >= 0:
                os.close(r_fd)
            os.close(w_fd)

    @pytest.mark.asyncio
    async def test_cleanup_agent_no_fd_registered(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Cleanup succeeds when no master_fd was registered."""
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id="run-no-fd",
            tmux_session_name="gobby-no-fd",
        )

        result = await monitor._cleanup_agent(run, terminal_payload="test cleanup", is_success=True)

        assert result is None
        assert run.id not in monitor._master_fds
