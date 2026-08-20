"""Additional tests for AgentLifecycleMonitor."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import pytest

from gobby.agents.idle_check_handler import IdleCheckHandler
from gobby.agents.idle_detector import IdleDetector
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.tmux import configure_tmux
from gobby.agents.watchdog import WatchdogReaderRegistry
from gobby.config.tmux import TmuxConfig as ConfiguredTmuxConfig
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._models import Task
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.workflows.step_instance_fixtures import make_step_instance
from gobby.workflows.task_claim_state import add_claimed_task

from .detection_test_support import BundledDetectionRegistry
from tests.agents.terminal_fixtures import make_live_terminal, make_pending_terminal

DETECTION_REGISTRY = BundledDetectionRegistry()
configure_tmux(ConfiguredTmuxConfig())

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _stage(state: str) -> dict[str, object]:
    return {"name": "development", "state": state, "position": 0}


def _task(
    *,
    task_id: str = "task-123",
    owner: str | None = "owner-1",
    stage_state: str = "in_progress",
    seq_num: int | None = 5,
    dispatch_failure_count: int = 0,
    closed_at: str | None = None,
) -> Task:
    return Task(
        id=task_id,
        project_id="project-1",
        title="test",
        priority=2,
        task_type="task",
        created_at="2024-01-01",
        updated_at="2024-01-01",
        claimed_by_session_id=owner,
        closed_at=closed_at,
        seq_num=seq_num,
        dispatch_failure_count=dispatch_failure_count,
        stages=(_stage(stage_state),),
    )


def _use_stall_classifier(monitor: AgentLifecycleMonitor, stall_classifier: MagicMock) -> None:
    stall_classifier.for_provider.return_value = stall_classifier
    monitor._task_recovery._stall_classifier = stall_classifier


class TestRecoverTaskFromFailedAgent:
    """Tests for _recover_task_from_failed_agent."""

    @pytest.mark.asyncio
    async def test_recover_task_with_task_id(self) -> None:
        """Task recovered using explicit task_id."""
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()
        mock_db = MagicMock()
        mock_stall = MagicMock()

        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=mock_db,
            task_manager=mock_task_mgr,
            check_interval_seconds=1.0,
        )
        _use_stall_classifier(monitor, mock_stall)

        # Setup mock db run
        db_run = AgentRun(
            id="run-1",
            parent_session_id="parent-1",
            child_session_id="owner-1",
            task_id="task-123",
            provider="claude",
            prompt="do it",
            status="error",
            error="API failed",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        mock_run_mgr.get.return_value = db_run

        # Setup mock task
        mock_task = _task(task_id="task-123", owner="owner-1", seq_num=5)
        mock_task_mgr.get_task.return_value = mock_task
        mock_stall.is_provider_error.return_value = True

        await monitor._recover_task_from_failed_agent("run-1")

        mock_task_mgr.get_task.assert_called_once_with("task-123")
        assert mock_task_mgr.get_task.call_count == 1
        assert mock_task_mgr.get_task.call_args is not None
        # Provider error: dispatch_failure_count unchanged (stays at 0)
        mock_task_mgr.release_task_claim.assert_called_once_with(
            "task-123", dispatch_failure_count=0
        )
        assert mock_task_mgr.release_task_claim.call_count == 1
        assert mock_task_mgr.release_task_claim.call_args is not None

    @pytest.mark.asyncio
    async def test_recover_task_fallback_claimed_session(self) -> None:
        """Task recovered using child_session_id as fallback."""
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()
        mock_db = MagicMock()
        mock_stall = MagicMock()

        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=mock_db,
            task_manager=mock_task_mgr,
        )
        _use_stall_classifier(monitor, mock_stall)

        # Setup mock db run (no task_id, but has child_session_id)
        db_run = AgentRun(
            id="run-2",
            parent_session_id="parent-1",
            child_session_id="child-123",
            task_id=None,
            provider="claude",
            prompt="do it",
            status="error",
            error="",
            created_at="2024-01-01",
            updated_at="2024-01-01T00:00:00Z",
        )
        mock_run_mgr.get.return_value = db_run

        mock_fallback_task = _task(
            task_id="task-fallback",
            owner="child-123",
            seq_num=None,
        )
        mock_task_mgr.list_tasks.return_value = [mock_fallback_task]
        mock_task_mgr.get_task.return_value = mock_fallback_task
        mock_stall.is_provider_error.return_value = False

        await monitor._recover_task_from_failed_agent("run-2")

        mock_task_mgr.list_tasks.assert_called_once_with(
            claimed_by_session_id="child-123",
            closed=False,
        )
        assert mock_task_mgr.list_tasks.call_count == 1
        assert mock_task_mgr.list_tasks.call_args is not None
        mock_task_mgr.get_task.assert_called_once_with("task-fallback")
        assert mock_task_mgr.get_task.call_count == 1
        assert mock_task_mgr.get_task.call_args is not None
        # Non-provider error: dispatch_failure_count incremented from 0 to 1
        mock_task_mgr.release_task_claim.assert_called_once_with(
            "task-fallback", dispatch_failure_count=1
        )
        assert mock_task_mgr.release_task_claim.call_count == 1
        assert mock_task_mgr.release_task_claim.call_args is not None

    @pytest.mark.asyncio
    async def test_recover_task_releases_review_claim_without_status_change(self) -> None:
        """Failed review agent should clear claimed_by_session_id without regressing status."""
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()
        mock_stall = MagicMock()

        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            task_manager=mock_task_mgr,
        )
        _use_stall_classifier(monitor, mock_stall)

        db_run = AgentRun(
            id="run-review",
            parent_session_id="p",
            child_session_id="reviewer-1",
            task_id="task-review",
            provider="claude",
            prompt="review it",
            status="error",
            error="agent crashed",
            created_at="2024-01-01",
            updated_at="2024-01-01",
        )
        mock_run_mgr.get.return_value = db_run

        mock_task = _task(
            task_id="task-review",
            owner="reviewer-1",
            stage_state="needs_review",
            seq_num=22,
        )
        mock_task_mgr.get_task.return_value = mock_task
        mock_stall.is_provider_error.return_value = False

        await monitor._recover_task_from_failed_agent("run-review")

        mock_task_mgr.release_task_claim.assert_called_once_with("task-review")
        assert mock_task_mgr.release_task_claim.call_count == 1
        assert mock_task_mgr.release_task_claim.call_args is not None

    @pytest.mark.asyncio
    async def test_recover_task_no_task_manager(self) -> None:
        """Does nothing if no task_manager is configured."""
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=MagicMock(),
            db=MagicMock(),
        )
        result = await monitor._recover_task_from_failed_agent("run-1")
        assert result is None
        assert monitor._task_manager is None

    @pytest.mark.asyncio
    async def test_recover_task_not_in_progress(self) -> None:
        """Does not recover task if it is not in_progress."""
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            task_manager=mock_task_mgr,
        )

        db_run = AgentRun(
            id="run-1",
            parent_session_id="p",
            task_id="task-123",
            provider="claude",
            prompt="p",
            status="error",
            created_at="2024-01-01",
            updated_at="2024-01-01T00:00:00Z",
        )
        mock_run_mgr.get.return_value = db_run

        mock_task = _task(
            task_id="task-123",
            owner="owner-1",
            closed_at="2024-01-02T00:00:00Z",
        )
        mock_task_mgr.get_task.return_value = mock_task

        await monitor._recover_task_from_failed_agent("run-1")
        mock_task_mgr.release_task_claim.assert_not_called()
        assert mock_task_mgr.release_task_claim.call_count == 0
        assert not mock_task_mgr.release_task_claim.called

    @pytest.mark.asyncio
    async def test_recover_task_escalates_after_three_failures(self) -> None:
        """Task set to 'escalated' after 3 non-provider failures, counter reset."""
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()
        mock_stall = MagicMock()

        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            task_manager=mock_task_mgr,
        )
        _use_stall_classifier(monitor, mock_stall)

        db_run = AgentRun(
            id="run-1",
            parent_session_id="p",
            child_session_id="owner-1",
            task_id="task-1",
            provider="claude",
            prompt="p",
            status="error",
            error="agent crashed",
            created_at="2024-01-01",
            updated_at="2024-01-01",
        )
        mock_run_mgr.get.return_value = db_run

        mock_task = _task(
            task_id="task-1",
            owner="owner-1",
            seq_num=10,
            dispatch_failure_count=2,
        )
        mock_task_mgr.get_task.return_value = mock_task
        mock_stall.is_provider_error.return_value = False

        await monitor._recover_task_from_failed_agent("run-1")

        mock_task_mgr.release_task_claim.assert_called_once_with(
            "task-1",
            dispatch_failure_count=0,
            escalated_at=ANY,
            escalation_reason="Failed 3 dispatch attempts",
        )
        assert mock_task_mgr.release_task_claim.call_count == 1
        assert mock_task_mgr.release_task_claim.call_args is not None

    @pytest.mark.asyncio
    async def test_recover_task_provider_error_not_counted(self) -> None:
        """Provider errors don't increment dispatch_failure_count."""
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()
        mock_stall = MagicMock()

        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            task_manager=mock_task_mgr,
        )
        _use_stall_classifier(monitor, mock_stall)

        db_run = AgentRun(
            id="run-1",
            parent_session_id="p",
            child_session_id="owner-1",
            task_id="task-1",
            provider="claude",
            prompt="p",
            status="error",
            error="rate limit exceeded",
            created_at="2024-01-01",
            updated_at="2024-01-01",
        )
        mock_run_mgr.get.return_value = db_run

        mock_task = _task(
            task_id="task-1",
            owner="owner-1",
            seq_num=10,
            dispatch_failure_count=2,
        )
        mock_task_mgr.get_task.return_value = mock_task
        mock_stall.is_provider_error.return_value = True  # Provider error

        await monitor._recover_task_from_failed_agent("run-1")

        # Should NOT block — provider errors are excluded
        mock_task_mgr.release_task_claim.assert_called_once_with("task-1", dispatch_failure_count=2)
        assert mock_task_mgr.release_task_claim.call_count == 1
        assert mock_task_mgr.release_task_claim.call_args is not None

    @pytest.mark.asyncio
    async def test_recover_task_ignores_persisted_claimed_session_when_child_missing(
        self,
    ) -> None:
        """Recovery should not release a task when the child session is unknown."""
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()
        mock_stall = MagicMock()

        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            task_manager=mock_task_mgr,
        )
        _use_stall_classifier(monitor, mock_stall)

        db_run = AgentRun(
            id="run-claim-owner",
            parent_session_id="parent-1",
            claimed_session_id="original-owner",
            task_id="task-1",
            provider="claude",
            prompt="p",
            status="error",
            error="agent crashed",
            created_at="2024-01-01",
            updated_at="2024-01-01",
        )
        mock_run_mgr.get.return_value = db_run

        mock_task = _task(
            task_id="task-1",
            owner="original-owner",
            seq_num=10,
        )
        mock_task_mgr.get_task.return_value = mock_task
        mock_stall.is_provider_error.return_value = False

        await monitor._recover_task_from_failed_agent("run-claim-owner")

        mock_task_mgr.release_task_claim.assert_not_called()
        assert mock_task_mgr.release_task_claim.call_count == 0
        assert not mock_task_mgr.release_task_claim.called

    @pytest.mark.asyncio
    async def test_cleanup_stale_pending_runs(self) -> None:
        """Tests that cleanup_stale_pending_runs calls the manager method correctly."""
        mock_run_mgr = MagicMock()
        mock_run_mgr.cleanup_stale_pending_runs.return_value = [
            "run-1",
            "run-2",
            "run-3",
            "run-4",
            "run-5",
        ]

        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
        )

        cleaned = await monitor.cleanup_stale_pending_runs()
        assert cleaned == 5
        mock_run_mgr.cleanup_stale_pending_runs.assert_called_once()

    @pytest.mark.asyncio
    async def test_idle_check_does_not_probe_parent_session_when_child_missing(self) -> None:
        db_run = AgentRun(
            id="run-1",
            parent_session_id="parent-session",
            child_session_id=None,
            provider="codex",
            prompt="test",
            status="running",
            created_at="2024-01-01T00:00:00+00:00",
            updated_at="2024-01-01T00:00:00+00:00",
            task_id="task-1",
            terminal_id="agent-run-1",
        )
        mock_run_mgr = MagicMock()
        mock_run_mgr.get.return_value = db_run
        session_manager = MagicMock()
        tmux = MagicMock()
        tmux.capture_pane = AsyncMock(return_value="working")
        idle_detector = IdleDetector(DETECTION_REGISTRY, "claude")
        cleanup_handler = AsyncMock()

        async def run_db(func, *args, **kwargs):
            return func(*args, **kwargs)

        handler = IdleCheckHandler(
            agent_run_manager=mock_run_mgr,
            db=MagicMock(spec=HubDatabase),
            get_session_manager=lambda: session_manager,
            tmux=tmux,
            idle_detector=idle_detector,
            prompt_detector=PromptDetector(DETECTION_REGISTRY, "codex"),
            stall_classifier=MagicMock(),
            watchdog_readers=WatchdogReaderRegistry(),
            cleanup_handler=cleanup_handler,
            tmux_config=SimpleNamespace(
                idle_timeout_seconds=60,
                idle_reprompt_delay_seconds=60,
                max_reprompt_attempts=2,
            ),
            run_db=run_db,
        )

        result = await handler._handle_idle_check(db_run)

        assert result == 0
        session_manager.get.assert_not_called()
        tmux.capture_pane.assert_awaited_once_with("agent-run-1", lines=15)
        assert idle_detector.get_state("run-1").reprompt_count == 0


class TestLoopPromptEscalation:
    """Tests for loop prompt counting and escalation in check_loop_prompts."""

    @staticmethod
    def _run(run_id: str = "run-1") -> AgentRun:
        return AgentRun(
            id=run_id,
            parent_session_id="p",
            provider="claude",
            prompt="p",
            status="running",
            created_at="2024-01-01",
            updated_at="2024-01-01",
            terminal_id="gobby-test",
            pid=12345,
        )

    @pytest.mark.asyncio
    async def test_dismisses_below_threshold(self) -> None:
        """Loop prompts are dismissed normally when count < threshold."""
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
        )
        monitor._tmux = mock_tmux

        run = self._run()
        mock_run_mgr.list_active_for_machine.return_value = [run]
        mock_tmux.capture_pane.return_value = "stuck in a loop\nContinue? (y/n)"
        mock_tmux.send_keys.return_value = True

        handled = await monitor.check_loop_prompts()
        assert handled == 1
        mock_tmux.send_keys.assert_called_once_with("gobby-test", PromptDetector.LOOP_DISMISS_KEYS)
        assert monitor._loop_tracker.get_count("run-1") == 1

    @pytest.mark.asyncio
    async def test_escalates_at_threshold(self) -> None:
        """After 3 successful dismissals, agent is killed."""
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
        )
        monitor._tmux = mock_tmux

        run = self._run()
        mock_run_mgr.list_active_for_machine.return_value = [run]
        mock_tmux.capture_pane.return_value = "stuck in a loop\nContinue? (y/n)"
        mock_tmux.send_keys.return_value = True

        # Pre-load 2 dismissals
        monitor._loop_tracker.record_dismissal("run-1")
        monitor._loop_tracker.record_dismissal("run-1")

        with patch.object(
            monitor, "_checkpoint_and_kill_looping_agent", new_callable=AsyncMock
        ) as mock_kill:
            await monitor.check_loop_prompts()
            mock_kill.assert_called_once_with(run)
            assert mock_kill.call_count == 1
            assert mock_kill.call_args is not None

        mock_tmux.send_keys.assert_called_once_with("gobby-test", PromptDetector.LOOP_DISMISS_KEYS)
        assert monitor._loop_tracker.get_count("run-1") == 3

    @pytest.mark.asyncio
    async def test_static_loop_prose_without_dialog_chrome_is_ignored(self) -> None:
        """Static prose matching loop terms never sends keys or escalates."""
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
        )
        monitor._tmux = mock_tmux

        run = self._run()
        mock_run_mgr.list_active_for_machine.return_value = [run]
        mock_tmux.capture_pane.return_value = "It seems like I'm stuck in a loop.\n"

        with patch.object(
            monitor, "_checkpoint_and_kill_looping_agent", new_callable=AsyncMock
        ) as mock_kill:
            for _ in range(3):
                handled = await monitor.check_loop_prompts()
                assert handled == 0

        mock_tmux.send_keys.assert_not_called()
        mock_kill.assert_not_called()
        assert monitor._loop_tracker.get_count("run-1") == 0

    @pytest.mark.asyncio
    async def test_deduplicates_same_loop_prompt_fingerprint(self) -> None:
        """A repeated visible loop prompt is dismissed once per run."""
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
        )
        monitor._tmux = mock_tmux
        now = 0.0
        monitor._terminal_prompt_monitor._monotonic = lambda: now

        run = self._run()
        mock_run_mgr.list_active_for_machine.return_value = [run]
        mock_tmux.capture_pane.return_value = "Potential loop detected\nContinue? (y/n)"
        mock_tmux.send_keys.return_value = True

        assert await monitor.check_loop_prompts() == 1
        now = 120.0
        assert await monitor.check_loop_prompts() == 0

        mock_tmux.send_keys.assert_called_once_with("gobby-test", PromptDetector.LOOP_DISMISS_KEYS)
        assert monitor._loop_tracker.get_count("run-1") == 1

    @pytest.mark.asyncio
    async def test_throttles_distinct_loop_prompts_by_minimum_interval(self) -> None:
        """Distinct loop prompts still cannot increment the count too quickly."""
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
        )
        monitor._tmux = mock_tmux
        now = 0.0
        monitor._terminal_prompt_monitor._monotonic = lambda: now

        run = self._run()
        mock_run_mgr.list_active_for_machine.return_value = [run]
        mock_tmux.capture_pane.side_effect = [
            "Potential loop detected\nContinue? (y/n)",
            "It seems to be stuck\nContinue? (y/n)",
        ]
        mock_tmux.send_keys.return_value = True

        assert await monitor.check_loop_prompts() == 1
        now = 30.0
        assert await monitor.check_loop_prompts() == 0

        mock_tmux.send_keys.assert_called_once_with("gobby-test", PromptDetector.LOOP_DISMISS_KEYS)
        assert monitor._loop_tracker.get_count("run-1") == 1

    @pytest.mark.asyncio
    async def test_send_failure_does_not_count_or_fingerprint_loop_prompt(self) -> None:
        """Loop dismissals are counted only after keys are actually sent."""
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
        )
        monitor._tmux = mock_tmux

        run = self._run()
        mock_run_mgr.list_active_for_machine.return_value = [run]
        pane_output = "Potential loop detected\nContinue? (y/n)"
        mock_tmux.capture_pane.return_value = pane_output
        mock_tmux.send_keys.return_value = False

        handled = await monitor.check_loop_prompts()

        assert handled == 0
        assert monitor._loop_tracker.get_count("run-1") == 0
        assert monitor._prompt_detector.was_loop_prompt_dismissed("run-1", pane_output) is False


class TestApprovalPromptAutoEnter:
    """Tests for approval prompt auto-enter handling."""

    @staticmethod
    def _run(
        run_id: str = "run-approval",
        terminal_id: str | None = "gobby-approval",
    ) -> AgentRun:
        return AgentRun(
            id=run_id,
            parent_session_id="p",
            provider="codex",
            prompt="p",
            status="running",
            created_at="2024-01-01",
            updated_at="2024-01-01",
            terminal_id=terminal_id,
            pid=12345,
        )

    @staticmethod
    def _monitor(mock_run_mgr: MagicMock, mock_tmux: AsyncMock) -> AgentLifecycleMonitor:
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
        )
        monitor._tmux = mock_tmux
        return monitor

    @pytest.mark.asyncio
    async def test_sends_enter_once_for_approval_prompt(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux)
        mock_run_mgr.list_active_for_machine.return_value = [self._run()]
        mock_tmux.capture_pane.return_value = (
            "Approval required\nPress Enter to approve this command\n"
        )
        mock_tmux.send_keys.return_value = True

        handled = await monitor.check_approval_prompts()

        assert handled == 1
        mock_tmux.send_keys.assert_called_once_with(
            "gobby-approval",
            PromptDetector.ENTER_KEY,
            literal=False,
        )

    @pytest.mark.asyncio
    async def test_sends_enter_for_codex_tui_confirmation_prompt(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux)
        mock_run_mgr.list_active_for_machine.return_value = [self._run()]
        mock_tmux.capture_pane.return_value = (
            "Tool call needs your approval. Reason: Request contains encrypted reasoning "
            "and a tool call; requires user confirmation to proceed.\n"
            "› 1. Allow   Run the tool and continue.\n"
            "  2. Cancel  Cancel this tool call\n"
            "enter to submit | esc to cancel\n"
        )
        mock_tmux.send_keys.return_value = True

        handled = await monitor.check_approval_prompts()

        assert handled == 1
        mock_tmux.send_keys.assert_called_once_with(
            "gobby-approval",
            PromptDetector.ENTER_KEY,
            literal=False,
        )

    @pytest.mark.asyncio
    async def test_same_approval_prompt_is_deduped(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux)
        mock_run_mgr.list_active_for_machine.return_value = [self._run()]
        mock_tmux.capture_pane.return_value = (
            "Approval required\nPress Enter to approve this command\n"
        )
        mock_tmux.send_keys.return_value = True

        handled_1 = await monitor.check_approval_prompts()
        handled_2 = await monitor.check_approval_prompts()

        assert handled_1 == 1
        assert handled_2 == 0
        mock_tmux.send_keys.assert_called_once()

    @pytest.mark.asyncio
    async def test_changed_approval_prompt_can_be_handled_again(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux)
        mock_run_mgr.list_active_for_machine.return_value = [self._run()]
        mock_tmux.capture_pane.side_effect = [
            "Approval required\nPress Enter to approve command A\n",
            "Approval required\nPress Enter to approve command B\n",
        ]
        mock_tmux.send_keys.return_value = True

        handled_1 = await monitor.check_approval_prompts()
        handled_2 = await monitor.check_approval_prompts()

        assert handled_1 == 1
        assert handled_2 == 1
        assert mock_tmux.send_keys.call_count == 2

    @pytest.mark.asyncio
    async def test_disabled_config_skips_approval_prompt_handling(self) -> None:
        from gobby.config.tmux import TmuxConfig

        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            tmux_config=TmuxConfig(auto_enter_approval_prompts=False),
        )
        monitor._tmux = mock_tmux
        mock_run_mgr.list_active_for_machine.return_value = [self._run()]

        handled = await monitor.check_approval_prompts()

        assert handled == 0
        mock_tmux.capture_pane.assert_not_called()
        mock_tmux.send_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_terminal_runs_are_ignored(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux)
        mock_run_mgr.list_active_for_machine.return_value = [
            self._run(run_id="run-no-tmux", terminal_id=None)
        ]

        handled = await monitor.check_approval_prompts()

        assert handled == 0
        mock_tmux.capture_pane.assert_not_called()
        mock_tmux.send_keys.assert_not_called()


class TestPeriodicAgentTerminalEnter:
    """Tests for provider-agnostic autonomous terminal Enter heartbeat."""

    @staticmethod
    def _run(
        run_id: str = "run-periodic",
        terminal_id: str | None = "gobby-periodic",
        provider: str = "codex",
        child_session_id: str | None = None,
    ) -> AgentRun:
        return AgentRun(
            id=run_id,
            parent_session_id="p",
            child_session_id=child_session_id,
            provider=provider,
            prompt="p",
            status="running",
            created_at="2024-01-01",
            updated_at="2024-01-01",
            terminal_id=terminal_id,
            pid=12345,
        )

    @staticmethod
    def _monitor(
        mock_run_mgr: MagicMock,
        mock_tmux: AsyncMock,
        *,
        enabled: bool = True,
        interval: int = 30,
        auto_enter_approval_prompts: bool = True,
        db: HubDatabase | None = None,
    ) -> AgentLifecycleMonitor:
        from gobby.config.tmux import TmuxConfig

        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=db or MagicMock(),
            tmux_config=TmuxConfig(
                auto_enter_approval_prompts=auto_enter_approval_prompts,
                auto_enter_agent_terminals=enabled,
                auto_enter_agent_interval_seconds=interval,
            ),
        )
        mock_tmux.capture_pane.return_value = ""
        monitor._tmux = mock_tmux
        monitor._terminal_prompt_monitor._get_tmux = lambda: mock_tmux
        return monitor

    @pytest.mark.asyncio
    async def test_sends_enter_to_all_active_terminal_providers(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux)
        mock_run_mgr.list_active_for_machine.return_value = [
            self._run(run_id="run-codex", terminal_id="gobby-codex", provider="codex"),
            self._run(run_id="run-claude", terminal_id="gobby-claude", provider="claude"),
            self._run(run_id="run-qwen", terminal_id="gobby-qwen", provider="qwen"),
        ]
        mock_tmux.send_keys.return_value = True

        handled = await monitor.check_periodic_enters()

        assert handled == 3
        assert mock_tmux.send_keys.call_args_list == [
            call("gobby-codex", PromptDetector.ENTER_KEY, literal=False),
            call("gobby-claude", PromptDetector.ENTER_KEY, literal=False),
            call("gobby-qwen", PromptDetector.ENTER_KEY, literal=False),
        ]

    @pytest.mark.asyncio
    async def test_periodic_enter_skips_approval_prompt_when_gate_disabled(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(
            mock_run_mgr,
            mock_tmux,
            interval=30,
            auto_enter_approval_prompts=False,
        )
        mock_run_mgr.list_active_for_machine.return_value = [self._run()]
        mock_tmux.capture_pane.return_value = (
            "Tool call needs your approval.\n"
            "› 1. Allow   Run the tool and continue.\n"
            "  2. Cancel  Cancel this tool call\n"
            "enter to submit | esc to cancel\n"
        )
        current_time = 100.0
        monitor._terminal_prompt_monitor._monotonic = lambda: current_time

        handled_1 = await monitor.check_periodic_enters()
        current_time = 131.0
        handled_2 = await monitor.check_periodic_enters()

        assert (handled_1, handled_2) == (0, 0)
        assert mock_tmux.capture_pane.call_count == 2
        mock_tmux.send_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_periodic_enter_skips_known_dialogs_owned_by_specific_handlers(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux)
        mock_run_mgr.list_active_for_machine.return_value = [
            self._run(run_id="run-trust", terminal_id="gobby-trust"),
            self._run(run_id="run-loop", terminal_id="gobby-loop"),
            self._run(run_id="run-normal", terminal_id="gobby-normal"),
        ]
        mock_tmux.capture_pane.side_effect = [
            "Do you trust the files in this folder?\n❯ 1. Trust Folder\n",
            "Potential loop detected. Continue anyway? (yes/no)\n",
            "ready for input\n",
        ]
        mock_tmux.send_keys.return_value = True

        handled = await monitor.check_periodic_enters()

        assert handled == 1
        mock_tmux.send_keys.assert_called_once_with(
            "gobby-normal",
            PromptDetector.ENTER_KEY,
            literal=False,
        )

    @pytest.mark.asyncio
    async def test_periodic_enter_reaches_active_step_workflow_agents(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, object],
    ) -> None:
        from gobby.storage.definitions.agents import AgentDefinitionManager

        child = session_manager.register(
            external_id="child-step-workflow",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=str(sample_project["id"]),
        )
        AgentDefinitionManager(temp_db).create(
            name="planner-steps",
            definition_json=json.dumps(
                {
                    "name": "planner-steps",
                    "version": "1.0",
                    "enabled": True,
                    "steps": [{"name": "plan", "status_message": "submit_for_review"}],
                    "exit_condition": "current_step == 'terminate'",
                }
            ),
            enabled=True,
        )
        AgentStepInstanceManager(temp_db).save(
            make_step_instance(
                child.id,
                agent_name="planner",
                current_step="plan",
            )
        )

        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux, db=temp_db)
        mock_run_mgr.list_active_for_machine.return_value = [
            self._run(child_session_id=child.id),
        ]
        mock_tmux.send_keys.return_value = True

        handled = await monitor.check_periodic_enters()

        assert handled == 1
        mock_tmux.send_keys.assert_called_once_with(
            "gobby-periodic",
            PromptDetector.ENTER_KEY,
            literal=False,
        )

    @pytest.mark.asyncio
    async def test_observes_queued_gobby_continuation_without_editor_keys(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux, interval=30)
        mock_run_mgr.list_active_for_machine.return_value = [
            self._run(run_id="run-claude", terminal_id="gobby-claude", provider="claude"),
        ]
        mock_tmux.capture_pane.return_value = (
            "  ❯ Continue working on your task. Your active Gobby step workflow is not complete.\n"
            "    Workflow: planner-steps. Current step: plan.\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯ Press up to edit queued messages\n"
        )
        mock_tmux.send_keys.return_value = True

        await monitor.check_queued_continuation_prompts()
        periodic_handled = await monitor.check_periodic_enters()

        assert periodic_handled == 1
        mock_tmux.send_keys.assert_called_once_with(
            "gobby-claude",
            PromptDetector.ENTER_KEY,
            literal=False,
        )

    @pytest.mark.asyncio
    async def test_queued_message_prompt_without_gobby_continuation_is_ignored(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux)
        mock_run_mgr.list_active_for_machine.return_value = [self._run()]
        mock_tmux.capture_pane.return_value = "Press up to edit queued messages\n"

        result = await monitor.check_queued_continuation_prompts()

        assert result == 0
        mock_tmux.send_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_periodic_enter_respects_interval_per_run(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux, interval=30)
        mock_run_mgr.list_active_for_machine.return_value = [self._run()]
        mock_tmux.send_keys.return_value = True
        current_time = 100.0
        monitor._terminal_prompt_monitor._monotonic = lambda: current_time

        handled_1 = await monitor.check_periodic_enters()
        current_time = 120.0
        handled_2 = await monitor.check_periodic_enters()
        current_time = 131.0
        handled_3 = await monitor.check_periodic_enters()

        assert (handled_1, handled_2, handled_3) == (1, 0, 1)
        assert mock_tmux.send_keys.call_count == 2

    @pytest.mark.asyncio
    async def test_approval_prompt_enter_defers_periodic_enter(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux, interval=30)
        mock_run_mgr.list_active_for_machine.return_value = [self._run()]
        mock_tmux.capture_pane.return_value = (
            "Approval required\nPress Enter to approve command A\n"
        )
        mock_tmux.send_keys.return_value = True
        current_time = 100.0
        monitor._terminal_prompt_monitor._monotonic = lambda: current_time

        approval_handled = await monitor.check_approval_prompts()
        periodic_handled = await monitor.check_periodic_enters()

        assert approval_handled == 1
        assert periodic_handled == 0
        mock_tmux.send_keys.assert_called_once()

    @pytest.mark.asyncio
    async def test_periodic_enter_can_be_disabled(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux, enabled=False)
        mock_run_mgr.list_active_for_machine.return_value = [self._run()]

        handled = await monitor.check_periodic_enters()

        assert handled == 0
        mock_tmux.send_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_periodic_enter_ignores_runs_without_tmux(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux)
        mock_run_mgr.list_active_for_machine.return_value = [
            self._run(run_id="run-no-tmux", terminal_id=None)
        ]

        handled = await monitor.check_periodic_enters()

        assert handled == 0
        mock_tmux.send_keys.assert_not_called()


class TestDispatchFailureCountCRUD:
    """Tests for dispatch_failure_count in task CRUD operations."""

    def test_task_has_dispatch_failure_count_field(self) -> None:
        """Task dataclass includes dispatch_failure_count defaulting to 0."""

        task = Task(
            id="t-1",
            project_id="p-1",
            title="test",
            priority=2,
            task_type="task",
            created_at="2024-01-01",
            updated_at="2024-01-01",
            stages=(_stage("ready"),),
        )
        assert task.dispatch_failure_count == 0

    def test_dispatch_failure_count_in_to_dict(self) -> None:
        """dispatch_failure_count appears in to_dict output."""

        task = Task(
            id="t-1",
            project_id="p-1",
            title="test",
            priority=2,
            task_type="task",
            created_at="2024-01-01",
            updated_at="2024-01-01",
            dispatch_failure_count=3,
            stages=(_stage("ready"),),
        )
        d = task.to_dict()
        assert d["dispatch_failure_count"] == 3

    def test_dispatch_failure_count_in_to_brief(self) -> None:
        """dispatch_failure_count appears in to_brief output."""

        task = Task(
            id="t-1",
            project_id="p-1",
            title="test",
            priority=2,
            task_type="task",
            created_at="2024-01-01",
            updated_at="2024-01-01",
            dispatch_failure_count=3,
            escalated_at="2024-01-02T00:00:00Z",
            escalation_reason="manual review",
            stages=(_stage("ready"),),
        )
        brief = task.to_brief()
        assert brief["dispatch_failure_count"] == 3

    def test_update_task_sets_dispatch_failure_count(
        self, temp_db: HubDatabase, sample_project: dict
    ) -> None:
        """update_task can set dispatch_failure_count."""

        mgr = LocalTaskManager(temp_db)
        task = mgr.create_task(
            title="test",
            task_type="task",
            project_id=sample_project["id"],
            validation_criteria="Dispatch failure count can be updated.",
        )
        updated = mgr.update_task(task.id, dispatch_failure_count=2)
        assert updated.dispatch_failure_count == 2

    def test_reopen_resets_dispatch_failure_count(
        self, temp_db: HubDatabase, sample_project: dict
    ) -> None:
        """Reopening a task resets dispatch_failure_count to 0."""

        mgr = LocalTaskManager(temp_db)
        task = mgr.create_task(
            title="test",
            task_type="task",
            project_id=sample_project["id"],
            validation_criteria="Reopening resets the dispatch failure count.",
        )
        # Set failure count and move out of open state
        mgr.update_task(task.id, dispatch_failure_count=3)
        mgr.escalate_task(task.id, reason="dispatch failures")
        # Reopen
        mgr.reopen_task(task.id)
        reopened = mgr.get_task(task.id)
        assert reopened.dispatch_failure_count == 0


class TestTerminalizeCancelledRun:
    """Tests for terminalize_cancelled_run."""

    @pytest.mark.asyncio
    async def test_reopens_in_progress_task_and_notifies(self) -> None:
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()
        mock_task_mgr.stage_states.get.return_value = None
        mock_completion_registry = MagicMock()
        mock_completion_registry.notify = AsyncMock()
        mock_session_mgr = MagicMock()

        run = AgentRun(
            id="run-cancel",
            parent_session_id="parent-1",
            child_session_id="child-1",
            task_id="task-1",
            provider="claude",
            prompt="cancel it",
            status="cancelled",
            terminal_reason="user_cancelled",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        mock_run_mgr.cancel.return_value = run

        task = _task(
            task_id="task-1",
            owner="child-1",
            stage_state="in_progress",
            seq_num=42,
        )
        mock_task_mgr.get_task.return_value = task

        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            session_manager=mock_session_mgr,
            completion_registry=mock_completion_registry,
            task_manager=mock_task_mgr,
        )

        with patch(
            "gobby.agents.terminal_cleanup.cleanup_merged_task_artifacts_after_agent_exit",
            return_value=[],
        ):
            transitioned = await monitor.terminalize_cancelled_run(
                "run-cancel",
                terminal_reason="user_cancelled",
            )

        assert transitioned is True
        mock_task_mgr.release_task_claim.assert_called_once_with("task-1")
        assert mock_task_mgr.release_task_claim.call_count == 1
        assert mock_task_mgr.release_task_claim.call_args is not None
        mock_completion_registry.notify.assert_awaited_once_with(
            "run-cancel",
            result={
                "status": "cancelled",
                "terminal_reason": "user_cancelled",
                "run_id": "run-cancel",
            },
            message="Agent run-cancel cancelled",
        )
        assert mock_completion_registry.notify.await_count == 1
        assert mock_completion_registry.notify.await_args is not None
        mock_session_mgr.update_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_clears_claim_without_status_change_for_review_task(self) -> None:
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()

        run = AgentRun(
            id="run-review",
            parent_session_id="parent-1",
            child_session_id="child-1",
            task_id="task-review",
            provider="claude",
            prompt="cancel review",
            status="cancelled",
            terminal_reason="user_cancelled",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        mock_run_mgr.cancel.return_value = run

        task = _task(
            task_id="task-review",
            owner="child-1",
            stage_state="needs_review",
            seq_num=7,
        )
        mock_task_mgr.get_task.return_value = task

        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            task_manager=mock_task_mgr,
        )

        transitioned = await monitor.terminalize_cancelled_run(
            "run-review",
            terminal_reason="user_cancelled",
        )

        assert transitioned is True
        mock_task_mgr.release_task_claim.assert_called_once_with("task-review")

    @pytest.mark.asyncio
    async def test_cancelled_task_linked_run_cleans_child_session_claim_state(
        self,
        temp_db: HubDatabase,
        sample_project: dict,
    ) -> None:
        session_manager = SessionManager(temp_db)
        parent = session_manager.register(
            external_id="parent-session",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        child = session_manager.register(
            external_id="child-session",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        task_manager = LocalTaskManager(temp_db)
        task = task_manager.create_task(
            project_id=sample_project["id"],
            title="Child work",
            task_type="task",
            category="code",
            validation_criteria="Test task completion is observable.",
        )
        task_manager.initialize_task_manifest(task.id, stage_names=["development"])
        task_manager.stage_states.start_stage(task.id, "development", by_session_id=child.id)
        claimed = task_manager.claim_task(task.id, child.id)

        session_vars = SessionVariableManager(temp_db)
        session_vars.merge_variables(
            child.id,
            add_claimed_task({}, claimed.id, f"#{claimed.seq_num}"),
        )
        workflow_instances = AgentStepInstanceManager(temp_db)
        workflow_instances.save(
            make_step_instance(
                child.id,
                agent_name="developer-workflow",
                current_step="implement",
                variables={"task_claimed": True},
            )
        )

        run_manager = LocalAgentRunManager(temp_db)
        run = run_manager.create(
            parent_session_id=parent.id,
            child_session_id=child.id,
            claimed_session_id=child.id,
            provider="claude",
            prompt="do work",
            run_id="dddddddd-dddd-4ddd-8ddd-dddddddd3001",
            task_id=task.id,
        )
        run_manager.start(run.id)
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=run_manager,
            db=temp_db,
            session_manager=session_manager,
            task_manager=task_manager,
        )

        transitioned = await monitor.terminalize_cancelled_run(
            run.id,
            terminal_reason="user_cancelled",
        )

        updated_task = task_manager.get_task(task.id)
        child_vars = session_vars.get_variables(child.id)

        assert transitioned is True
        assert updated_task is not None
        assert updated_task.claimed_by_session_id is None
        assert child_vars["task_claimed"] is False
        assert child_vars["claimed_tasks"] == {}
        assert workflow_instances.get_for_session(child.id) is None

    @pytest.mark.asyncio
    async def test_cancelled_run_preserves_replacement_claim_and_cleans_old_child_state(
        self,
        temp_db: HubDatabase,
        sample_project: dict,
    ) -> None:
        session_manager = SessionManager(temp_db)
        parent = session_manager.register(
            external_id="parent-session",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        old_child = session_manager.register(
            external_id="old-child-session",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        replacement = session_manager.register(
            external_id="replacement-session",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        task_manager = LocalTaskManager(temp_db)
        task = task_manager.create_task(
            project_id=sample_project["id"],
            title="Replacement work",
            task_type="task",
            category="code",
            validation_criteria="Test task completion is observable.",
        )
        task_manager.initialize_task_manifest(task.id, stage_names=["development"])
        task_manager.stage_states.start_stage(task.id, "development", by_session_id=old_child.id)
        claimed = task_manager.claim_task(task.id, replacement.id)

        session_vars = SessionVariableManager(temp_db)
        session_vars.merge_variables(
            old_child.id,
            add_claimed_task({}, claimed.id, f"#{claimed.seq_num}"),
        )
        session_vars.merge_variables(
            replacement.id,
            add_claimed_task({}, claimed.id, f"#{claimed.seq_num}"),
        )
        AgentStepInstanceManager(temp_db).save(
            make_step_instance(
                old_child.id,
                agent_name="developer-workflow",
                current_step="implement",
                variables={"task_claimed": True},
            )
        )

        run_manager = LocalAgentRunManager(temp_db)
        run = run_manager.create(
            parent_session_id=parent.id,
            child_session_id=old_child.id,
            claimed_session_id=old_child.id,
            provider="claude",
            prompt="old work",
            run_id="dddddddd-dddd-4ddd-8ddd-dddddddd3002",
            task_id=task.id,
        )
        run_manager.start(run.id)
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=run_manager,
            db=temp_db,
            session_manager=session_manager,
            task_manager=task_manager,
        )

        transitioned = await monitor.terminalize_cancelled_run(
            run.id,
            terminal_reason="user_cancelled",
        )

        updated_task = task_manager.get_task(task.id)
        old_vars = session_vars.get_variables(old_child.id)
        replacement_vars = session_vars.get_variables(replacement.id)

        assert transitioned is True
        assert updated_task is not None
        assert updated_task.claimed_by_session_id == replacement.id
        assert old_vars["task_claimed"] is False
        assert old_vars["claimed_tasks"] == {}
        assert replacement_vars["task_claimed"] is True
        assert replacement_vars["claimed_tasks"] == {claimed.id: f"#{claimed.seq_num}"}
        assert AgentStepInstanceManager(temp_db).get_for_session(old_child.id) is None

    @pytest.mark.asyncio
    async def test_no_second_notification_when_run_already_terminal(self) -> None:
        mock_run_mgr = MagicMock()
        mock_run_mgr.cancel.return_value = None
        mock_run_mgr.get.return_value = AgentRun(
            id="run-done",
            parent_session_id="parent-1",
            provider="claude",
            prompt="done",
            status="success",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        mock_completion_registry = MagicMock()
        mock_completion_registry.notify = AsyncMock()

        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            completion_registry=mock_completion_registry,
        )

        transitioned = await monitor.terminalize_cancelled_run(
            "run-done",
            terminal_reason="user_cancelled",
        )

        assert transitioned is False
        mock_completion_registry.notify.assert_not_awaited()
        assert mock_completion_registry.notify.await_count == 0
        assert mock_completion_registry.notify.await_args is None

    @pytest.mark.asyncio
    async def test_daemon_stop_orphan_reaper_performs_full_cleanup(self) -> None:
        run = AgentRun(
            id="run-orphan",
            parent_session_id="parent-1",
            child_session_id="child-1",
            task_id="task-1",
            provider="codex",
            prompt="resume after restart",
            status="cancelled",
            terminal_reason="daemon_stop",
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
            updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        run_manager = MagicMock()
        run_manager.list_daemon_stop_orphans.return_value = [run]
        monitor = AgentLifecycleMonitor(
            detection_registry=cast(Any, DETECTION_REGISTRY),
            agent_run_manager=run_manager,
            db=MagicMock(),
            task_manager=MagicMock(),
        )
        recover_task = AsyncMock()
        full_cleanup = AsyncMock()

        with (
            patch.object(
                monitor._task_recovery,
                "recover_task_from_terminal_agent",
                new=recover_task,
            ),
            patch.object(
                monitor._cleanup_handler,
                "post_terminal_cleanup",
                new=full_cleanup,
            ),
            patch(
                "gobby.storage.agent_resume.claim_daemon_stop_orphan_reap",
                return_value=True,
            ) as claim,
            patch(
                "gobby.storage.agent_resume.expire_parked_daemon_session",
                return_value=True,
            ) as expire,
        ):
            reaped = await monitor.reap_daemon_stop_orphans()

        assert reaped == 1
        claim.assert_called_once()
        assert claim.call_count == 1
        recover_task.assert_awaited_once_with(
            run,
            outcome="cancelled",
        )
        assert recover_task.await_count == 1
        full_cleanup.assert_awaited_once_with(
            run,
            cleanup_session_id="child-1",
            notification_result={
                "status": "cancelled",
                "terminal_reason": "daemon_stop",
                "run_id": "run-orphan",
            },
            notification_message="Agent run-orphan recovery window expired",
            force_full_cleanup=True,
        )
        assert full_cleanup.await_count == 1
        expire.assert_called_once()
        assert expire.call_count == 1
        run_manager.merge_resume_metadata.assert_called_once()
        assert run_manager.merge_resume_metadata.call_count == 1
