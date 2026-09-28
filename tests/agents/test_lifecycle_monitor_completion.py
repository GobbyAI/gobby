"""Completion-specific lifecycle monitor regressions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun

from .detection_test_support import BundledDetectionRegistry

DETECTION_REGISTRY = BundledDetectionRegistry()
pytestmark = pytest.mark.unit


class TestCompletedRunIdleGuard:
    @pytest.mark.asyncio
    async def test_handle_idle_check_skips_run_completed_in_db(self) -> None:
        agent_run_manager = MagicMock()
        latest_run = SimpleNamespace(id="run-123", status="completed", provider="claude")
        agent_run_manager.get.return_value = latest_run
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=MagicMock(),
            check_interval_seconds=1.0,
            tmux_config=TmuxConfig(
                idle_check_enabled=True,
                idle_timeout_seconds=10,
                max_reprompt_attempts=2,
            ),
        )
        stale_run = cast(
            AgentRun,
            SimpleNamespace(
                id="run-123",
                status="running",
                provider="claude",
                terminal_id="gobby-run-123",
                child_session_id="child-123",
                parent_session_id="parent-123",
            ),
        )

        handled = await monitor._idle_check_handler._handle_idle_check(stale_run)

        assert handled == 0
        agent_run_manager.get.assert_called_once_with("run-123")
