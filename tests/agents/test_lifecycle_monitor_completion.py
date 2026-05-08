"""Completion-specific lifecycle monitor regressions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.config.tmux import TmuxConfig

pytestmark = pytest.mark.unit


class TestCompletedRunIdleGuard:
    @pytest.mark.asyncio
    async def test_handle_idle_check_skips_run_completed_in_db(self) -> None:
        agent_run_manager = MagicMock()
        agent_run_manager.get.return_value = MagicMock(id="run-123", status="completed")
        monitor = AgentLifecycleMonitor(
            agent_run_manager=agent_run_manager,
            db=MagicMock(),
            check_interval_seconds=1.0,
            tmux_config=TmuxConfig(
                idle_check_enabled=True,
                idle_timeout_seconds=10,
                max_reprompt_attempts=2,
            ),
        )
        stale_run = MagicMock(
            id="run-123",
            status="running",
            tmux_session_name="gobby-run-123",
            child_session_id="child-123",
            parent_session_id="parent-123",
        )

        with (
            patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock) as mock_capture,
            patch.object(monitor._tmux, "send_keys", new_callable=AsyncMock) as mock_send,
        ):
            handled = await monitor._idle_check_handler._handle_idle_check(stale_run)

        assert handled == 0
        mock_capture.assert_not_awaited()
        assert mock_capture.await_count == 0
        assert mock_capture.await_args is None
        mock_send.assert_not_awaited()
        assert mock_send.await_count == 0
        assert mock_send.await_args is None
