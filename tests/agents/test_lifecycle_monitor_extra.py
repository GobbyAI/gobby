"""Additional tests for AgentLifecycleMonitor."""

from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import pytest

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.prompt_detector import PromptDetector
from gobby.storage.agents import AgentRun
from gobby.storage.database import LocalDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._models import Task

pytestmark = pytest.mark.unit


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
        # Provider error: dispatch_failure_count unchanged (stays at 0)
        mock_task_mgr.release_task_claim.assert_called_once_with(
            "task-123", dispatch_failure_count=0
        )

    @pytest.mark.asyncio
    async def test_recover_task_fallback_assignee(self) -> None:
        """Task recovered using child_session_id as fallback."""
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()
        mock_db = MagicMock()
        mock_stall = MagicMock()

        monitor = AgentLifecycleMonitor(
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
        mock_task_mgr.get_task.assert_called_once_with("task-fallback")
        # Non-provider error: dispatch_failure_count incremented from 0 to 1
        mock_task_mgr.release_task_claim.assert_called_once_with(
            "task-fallback", dispatch_failure_count=1
        )

    @pytest.mark.asyncio
    async def test_recover_task_releases_review_claim_without_status_change(self) -> None:
        """Failed review agent should clear assignee without regressing status."""
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()
        mock_stall = MagicMock()

        monitor = AgentLifecycleMonitor(
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            task_manager=mock_task_mgr,
        )
        _use_stall_classifier(monitor, mock_stall)

        db_run = AgentRun(
            id="run-review",
            parent_session_id="p",
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

    @pytest.mark.asyncio
    async def test_recover_task_no_task_manager(self) -> None:
        """Does nothing if no task_manager is configured."""
        monitor = AgentLifecycleMonitor(
            agent_run_manager=MagicMock(),
            db=MagicMock(),
        )
        await monitor._recover_task_from_failed_agent("run-1")
        # Should return safely

    @pytest.mark.asyncio
    async def test_recover_task_not_in_progress(self) -> None:
        """Does not recover task if it is not in_progress."""
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()
        monitor = AgentLifecycleMonitor(
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

    @pytest.mark.asyncio
    async def test_recover_task_escalates_after_three_failures(self) -> None:
        """Task set to 'escalated' after 3 non-provider failures, counter reset."""
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()
        mock_stall = MagicMock()

        monitor = AgentLifecycleMonitor(
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            task_manager=mock_task_mgr,
        )
        _use_stall_classifier(monitor, mock_stall)

        db_run = AgentRun(
            id="run-1",
            parent_session_id="p",
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

    @pytest.mark.asyncio
    async def test_recover_task_provider_error_not_counted(self) -> None:
        """Provider errors don't increment dispatch_failure_count."""
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()
        mock_stall = MagicMock()

        monitor = AgentLifecycleMonitor(
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            task_manager=mock_task_mgr,
        )
        _use_stall_classifier(monitor, mock_stall)

        db_run = AgentRun(
            id="run-1",
            parent_session_id="p",
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

    @pytest.mark.asyncio
    async def test_recover_task_uses_persisted_claimed_session_id(self) -> None:
        """Recovery should not release a task claimed by a different live session."""
        mock_run_mgr = MagicMock()
        mock_task_mgr = MagicMock()
        mock_stall = MagicMock()

        monitor = AgentLifecycleMonitor(
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
            owner="different-owner",
            seq_num=10,
        )
        mock_task_mgr.get_task.return_value = mock_task
        mock_stall.is_provider_error.return_value = False

        await monitor._recover_task_from_failed_agent("run-claim-owner")

        mock_task_mgr.release_task_claim.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_stale_pending_runs(self) -> None:
        """Tests that cleanup_stale_pending_runs calls the manager method correctly."""
        mock_run_mgr = MagicMock()
        mock_run_mgr.cleanup_stale_pending_runs.return_value = 5

        monitor = AgentLifecycleMonitor(
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
        )

        cleaned = await monitor.cleanup_stale_pending_runs()
        assert cleaned == 5
        mock_run_mgr.cleanup_stale_pending_runs.assert_called_once()


class TestLoopPromptEscalation:
    """Tests for loop prompt counting and escalation in check_loop_prompts."""

    @pytest.mark.asyncio
    async def test_dismisses_below_threshold(self) -> None:
        """Loop prompts are dismissed normally when count < threshold."""
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = AgentLifecycleMonitor(
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
        )
        monitor._tmux = mock_tmux

        run = AgentRun(
            id="run-1",
            parent_session_id="p",
            provider="claude",
            prompt="p",
            status="running",
            created_at="2024-01-01",
            updated_at="2024-01-01",
            tmux_session_name="gobby-test",
            pid=12345,
        )
        mock_run_mgr.list_active.return_value = [run]
        mock_tmux.capture_pane.return_value = "stuck in a loop\nContinue? (y/n)"
        mock_tmux.send_keys.return_value = True

        handled = await monitor.check_loop_prompts()
        assert handled == 1
        mock_tmux.send_keys.assert_called_once_with("gobby-test", PromptDetector.LOOP_DISMISS_KEYS)
        assert monitor._loop_tracker.get_count("run-1") == 1

    @pytest.mark.asyncio
    async def test_escalates_at_threshold(self) -> None:
        """After 3 dismissals, agent is killed instead of dismissed."""
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = AgentLifecycleMonitor(
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
        )
        monitor._tmux = mock_tmux

        run = AgentRun(
            id="run-1",
            parent_session_id="p",
            provider="claude",
            prompt="p",
            status="running",
            created_at="2024-01-01",
            updated_at="2024-01-01",
            tmux_session_name="gobby-test",
            pid=12345,
        )
        mock_run_mgr.list_active.return_value = [run]
        mock_tmux.capture_pane.return_value = "stuck in a loop"

        # Pre-load 2 dismissals
        monitor._loop_tracker.record_dismissal("run-1")
        monitor._loop_tracker.record_dismissal("run-1")

        with patch.object(
            monitor, "_checkpoint_and_kill_looping_agent", new_callable=AsyncMock
        ) as mock_kill:
            await monitor.check_loop_prompts()
            mock_kill.assert_called_once_with(run)

        # send_keys should NOT have been called (escalated instead)
        mock_tmux.send_keys.assert_not_called()


class TestApprovalPromptAutoEnter:
    """Tests for approval prompt auto-enter handling."""

    @staticmethod
    def _run(
        run_id: str = "run-approval",
        tmux_session_name: str | None = "gobby-approval",
    ) -> AgentRun:
        return AgentRun(
            id=run_id,
            parent_session_id="p",
            provider="codex",
            prompt="p",
            status="running",
            created_at="2024-01-01",
            updated_at="2024-01-01",
            tmux_session_name=tmux_session_name,
            pid=12345,
        )

    @staticmethod
    def _monitor(mock_run_mgr: MagicMock, mock_tmux: AsyncMock) -> AgentLifecycleMonitor:
        monitor = AgentLifecycleMonitor(
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
        mock_run_mgr.list_active.return_value = [self._run()]
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
        mock_run_mgr.list_active.return_value = [self._run()]
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
        mock_run_mgr.list_active.return_value = [self._run()]
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
        mock_run_mgr.list_active.return_value = [self._run()]
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
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            tmux_config=TmuxConfig(auto_enter_approval_prompts=False),
        )
        monitor._tmux = mock_tmux
        mock_run_mgr.list_active.return_value = [self._run()]

        handled = await monitor.check_approval_prompts()

        assert handled == 0
        mock_tmux.capture_pane.assert_not_called()
        mock_tmux.send_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_terminal_runs_are_ignored(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux)
        mock_run_mgr.list_active.return_value = [
            self._run(run_id="run-no-tmux", tmux_session_name=None)
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
        tmux_session_name: str | None = "gobby-periodic",
        provider: str = "codex",
    ) -> AgentRun:
        return AgentRun(
            id=run_id,
            parent_session_id="p",
            provider=provider,
            prompt="p",
            status="running",
            created_at="2024-01-01",
            updated_at="2024-01-01",
            tmux_session_name=tmux_session_name,
            pid=12345,
        )

    @staticmethod
    def _monitor(
        mock_run_mgr: MagicMock,
        mock_tmux: AsyncMock,
        *,
        enabled: bool = True,
        interval: int = 30,
    ) -> AgentLifecycleMonitor:
        from gobby.config.tmux import TmuxConfig

        monitor = AgentLifecycleMonitor(
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            tmux_config=TmuxConfig(
                auto_enter_agent_terminals=enabled,
                auto_enter_agent_interval_seconds=interval,
            ),
        )
        monitor._tmux = mock_tmux
        monitor._terminal_prompt_monitor._get_tmux = lambda: mock_tmux
        return monitor

    @pytest.mark.asyncio
    async def test_sends_enter_to_all_active_terminal_providers(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux)
        mock_run_mgr.list_active.return_value = [
            self._run(run_id="run-codex", tmux_session_name="gobby-codex", provider="codex"),
            self._run(run_id="run-claude", tmux_session_name="gobby-claude", provider="claude"),
            self._run(run_id="run-gemini", tmux_session_name="gobby-gemini", provider="gemini"),
        ]
        mock_tmux.send_keys.return_value = True

        handled = await monitor.check_periodic_enters()

        assert handled == 3
        assert mock_tmux.send_keys.call_args_list == [
            call("gobby-codex", PromptDetector.ENTER_KEY, literal=False),
            call("gobby-claude", PromptDetector.ENTER_KEY, literal=False),
            call("gobby-gemini", PromptDetector.ENTER_KEY, literal=False),
        ]

    @pytest.mark.asyncio
    async def test_periodic_enter_respects_interval_per_run(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux, interval=30)
        mock_run_mgr.list_active.return_value = [self._run()]
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
        mock_run_mgr.list_active.return_value = [self._run()]
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
        mock_run_mgr.list_active.return_value = [self._run()]

        handled = await monitor.check_periodic_enters()

        assert handled == 0
        mock_tmux.send_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_periodic_enter_ignores_runs_without_tmux(self) -> None:
        mock_run_mgr = MagicMock()
        mock_tmux = AsyncMock()
        monitor = self._monitor(mock_run_mgr, mock_tmux)
        mock_run_mgr.list_active.return_value = [
            self._run(run_id="run-no-tmux", tmux_session_name=None)
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
        self, temp_db: LocalDatabase, sample_project: dict
    ) -> None:
        """update_task can set dispatch_failure_count."""

        mgr = LocalTaskManager(temp_db)
        task = mgr.create_task(title="test", task_type="task", project_id=sample_project["id"])
        updated = mgr.update_task(task.id, dispatch_failure_count=2)
        assert updated.dispatch_failure_count == 2

    def test_reopen_resets_dispatch_failure_count(
        self, temp_db: LocalDatabase, sample_project: dict
    ) -> None:
        """Reopening a task resets dispatch_failure_count to 0."""

        mgr = LocalTaskManager(temp_db)
        task = mgr.create_task(title="test", task_type="task", project_id=sample_project["id"])
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
            agent_run_manager=mock_run_mgr,
            db=MagicMock(),
            session_manager=mock_session_mgr,
            completion_registry=mock_completion_registry,
            task_manager=mock_task_mgr,
        )

        transitioned = await monitor.terminalize_cancelled_run(
            "run-cancel",
            terminal_reason="user_cancelled",
        )

        assert transitioned is True
        mock_task_mgr.release_task_claim.assert_called_once_with("task-1")
        mock_completion_registry.notify.assert_awaited_once_with(
            "run-cancel",
            result={
                "status": "cancelled",
                "terminal_reason": "user_cancelled",
                "run_id": "run-cancel",
            },
            message="Agent run-cancel cancelled",
        )
        mock_session_mgr.update_status.assert_called_once_with("child-1", "expired")

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
