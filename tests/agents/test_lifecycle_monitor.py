"""Tests for gobby.agents.lifecycle_monitor module.

Tests for the AgentLifecycleMonitor that detects dead tmux sessions
and completed/failed autonomous tasks, and marks their agent DB records.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.registry import RunningAgent, RunningAgentRegistry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.database import LocalDatabase
from gobby.storage.sessions import LocalSessionManager

pytestmark = pytest.mark.unit


@pytest.fixture
def registry() -> RunningAgentRegistry:
    return RunningAgentRegistry()


@pytest.fixture
def agent_run_manager(temp_db: LocalDatabase) -> LocalAgentRunManager:
    return LocalAgentRunManager(temp_db)


@pytest.fixture
def sample_session(
    session_manager: LocalSessionManager,
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
    registry: RunningAgentRegistry,
    agent_run_manager: LocalAgentRunManager,
) -> AgentLifecycleMonitor:
    return AgentLifecycleMonitor(
        agent_registry=registry,
        agent_run_manager=agent_run_manager,
        check_interval_seconds=1.0,
    )


def _make_terminal_agent(
    registry: RunningAgentRegistry,
    run_id: str = "run-abc123",
    session_id: str = "sess-child",
    parent_session_id: str = "sess-parent",
    tmux_session_name: str = "gobby-1234567890-abc123",
) -> RunningAgent:
    """Helper to create and register a terminal-mode agent."""
    agent = RunningAgent(
        run_id=run_id,
        session_id=session_id,
        parent_session_id=parent_session_id,
        mode="terminal",
        tmux_session_name=tmux_session_name,
        started_at=datetime.now(UTC),
    )
    registry.add(agent)
    return agent


def _make_autonomous_agent(
    registry: RunningAgentRegistry,
    run_id: str = "run-auto",
    session_id: str = "sess-auto",
    parent_session_id: str = "sess-parent",
    task: asyncio.Task | None = None,  # type: ignore[type-arg]
) -> RunningAgent:
    """Helper to create and register an autonomous-mode agent with an asyncio.Task."""
    agent = RunningAgent(
        run_id=run_id,
        session_id=session_id,
        parent_session_id=parent_session_id,
        mode="autonomous",
        started_at=datetime.now(UTC),
        task=task,
    )
    registry.add(agent)
    return agent


class TestCheckDeadAgents:
    """Tests for check_dead_agents."""

    @pytest.mark.asyncio
    async def test_detects_dead_tmux_session(
        self,
        monitor: AgentLifecycleMonitor,
        registry: RunningAgentRegistry,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Dead tmux session is detected and agent run marked as failed."""
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-dead",
        )
        agent_run_manager.start(run.id)
        _make_terminal_agent(registry, run_id=run.id, tmux_session_name="gobby-dead")

        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            cleaned = await monitor.check_dead_agents()

        assert cleaned == 1
        assert registry.get(run.id) is None

        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"
        assert "tmux session died" in (updated.error or "")

    @pytest.mark.asyncio
    async def test_skips_alive_tmux_session(
        self,
        monitor: AgentLifecycleMonitor,
        registry: RunningAgentRegistry,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Alive tmux session is left untouched."""
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-alive",
        )
        agent_run_manager.start(run.id)
        _make_terminal_agent(registry, run_id=run.id, tmux_session_name="gobby-alive")

        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=True):
            cleaned = await monitor.check_dead_agents()

        assert cleaned == 0
        assert registry.get(run.id) is not None

        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_no_tmux_agents_returns_zero(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """Returns 0 when no terminal agents are in the registry."""
        cleaned = await monitor.check_dead_agents()
        assert cleaned == 0

    @pytest.mark.asyncio
    async def test_skips_non_terminal_agents(
        self,
        monitor: AgentLifecycleMonitor,
        registry: RunningAgentRegistry,
    ) -> None:
        """Non-terminal agents (autonomous, in_process) are not checked."""
        agent = RunningAgent(
            run_id="run-autonomous",
            session_id="sess-1",
            parent_session_id="sess-parent",
            mode="autonomous",
            tmux_session_name=None,
        )
        registry.add(agent)

        cleaned = await monitor.check_dead_agents()
        assert cleaned == 0
        assert registry.get("run-autonomous") is not None

    @pytest.mark.asyncio
    async def test_skips_already_completed_db_record(
        self,
        monitor: AgentLifecycleMonitor,
        registry: RunningAgentRegistry,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """If DB record is already in terminal state, only cleans registry."""
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-done",
        )
        agent_run_manager.start(run.id)
        agent_run_manager.complete(run.id, result="done")
        _make_terminal_agent(registry, run_id=run.id, tmux_session_name="gobby-done")

        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            cleaned = await monitor.check_dead_agents()

        assert cleaned == 1
        assert registry.get(run.id) is None
        # DB status should remain 'success', not overwritten
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "success"

    @pytest.mark.asyncio
    async def test_handles_tmux_check_error(
        self,
        monitor: AgentLifecycleMonitor,
        registry: RunningAgentRegistry,
    ) -> None:
        """Errors checking tmux are caught per-agent, don't crash the loop."""
        _make_terminal_agent(registry, run_id="run-err", tmux_session_name="gobby-err")

        with patch.object(
            monitor._tmux,
            "has_session",
            new_callable=AsyncMock,
            side_effect=OSError("tmux socket gone"),
        ):
            cleaned = await monitor.check_dead_agents()

        assert cleaned == 0
        # Agent stays in registry since we couldn't determine its status
        assert registry.get("run-err") is not None

    @pytest.mark.asyncio
    async def test_releases_worktrees_on_dead_agent(
        self,
        registry: RunningAgentRegistry,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Worktrees are released when a dead agent is cleaned up."""
        mock_coordinator = MagicMock()
        mon = AgentLifecycleMonitor(
            agent_registry=registry,
            agent_run_manager=agent_run_manager,
            session_coordinator=mock_coordinator,
        )

        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-wt",
        )
        agent_run_manager.start(run.id)
        agent = _make_terminal_agent(
            registry, run_id=run.id, session_id="sess-wt", tmux_session_name="gobby-wt"
        )

        with patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            await mon.check_dead_agents()

        mock_coordinator.release_session_worktrees.assert_called_once_with(agent.session_id)


class TestCheckDeadAutonomousAgents:
    """Tests for autonomous/in_process task-based agent detection in check_dead_agents."""

    @pytest.mark.asyncio
    async def test_detects_completed_autonomous_task(
        self,
        monitor: AgentLifecycleMonitor,
        registry: RunningAgentRegistry,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Completed autonomous task is detected and agent run marked as success."""
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-auto-done",
        )
        agent_run_manager.start(run.id)

        # Create a done task (completed successfully)
        async def _ok() -> str:
            return "result"

        done_task: asyncio.Task[str] = asyncio.ensure_future(_ok())
        await done_task  # Let it finish

        _make_autonomous_agent(registry, run_id=run.id, task=done_task)

        cleaned = await monitor.check_dead_agents()

        assert cleaned == 1
        assert registry.get(run.id) is None

        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "success"

    @pytest.mark.asyncio
    async def test_detects_failed_autonomous_task(
        self,
        monitor: AgentLifecycleMonitor,
        registry: RunningAgentRegistry,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Failed autonomous task is detected and agent run marked as error."""
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-auto-fail",
        )
        agent_run_manager.start(run.id)

        # Create a done task that raised an exception
        async def _failing() -> str:
            raise RuntimeError("SDK connection lost")

        failed_task: asyncio.Task[str] = asyncio.ensure_future(_failing())
        try:
            await failed_task
        except RuntimeError:
            pass

        _make_autonomous_agent(registry, run_id=run.id, task=failed_task)

        cleaned = await monitor.check_dead_agents()

        assert cleaned == 1
        assert registry.get(run.id) is None

        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"
        assert "SDK connection lost" in (updated.error or "")

    @pytest.mark.asyncio
    async def test_detects_cancelled_autonomous_task(
        self,
        monitor: AgentLifecycleMonitor,
        registry: RunningAgentRegistry,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Cancelled autonomous task is detected and cleaned up."""
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-auto-cancel",
        )
        agent_run_manager.start(run.id)

        # Create a cancelled task
        async def _hang() -> str:
            await asyncio.sleep(3600)
            return "never"

        cancel_task: asyncio.Task[str] = asyncio.ensure_future(_hang())
        cancel_task.cancel()
        try:
            await cancel_task
        except asyncio.CancelledError:
            pass

        _make_autonomous_agent(registry, run_id=run.id, task=cancel_task)

        cleaned = await monitor.check_dead_agents()

        assert cleaned == 1
        assert registry.get(run.id) is None

        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"
        assert "cancelled" in (updated.error or "").lower()

    @pytest.mark.asyncio
    async def test_skips_still_running_autonomous_task(
        self,
        monitor: AgentLifecycleMonitor,
        registry: RunningAgentRegistry,
    ) -> None:
        """Still-running autonomous tasks are left untouched."""
        async def _long_running() -> str:
            await asyncio.sleep(3600)
            return "done"

        running_task: asyncio.Task[str] = asyncio.ensure_future(_long_running())
        _make_autonomous_agent(registry, run_id="run-still-going", task=running_task)

        cleaned = await monitor.check_dead_agents()

        assert cleaned == 0
        assert registry.get("run-still-going") is not None
        running_task.cancel()
        try:
            await running_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_autonomous_agent_without_task_is_skipped(
        self,
        monitor: AgentLifecycleMonitor,
        registry: RunningAgentRegistry,
    ) -> None:
        """Autonomous agents with no task field are skipped."""
        _make_autonomous_agent(registry, run_id="run-no-task", task=None)

        cleaned = await monitor.check_dead_agents()

        assert cleaned == 0
        assert registry.get("run-no-task") is not None

    @pytest.mark.asyncio
    async def test_releases_worktrees_on_completed_autonomous(
        self,
        registry: RunningAgentRegistry,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Worktrees are released when a completed autonomous agent is cleaned up."""
        mock_coordinator = MagicMock()
        mon = AgentLifecycleMonitor(
            agent_registry=registry,
            agent_run_manager=agent_run_manager,
            session_coordinator=mock_coordinator,
        )

        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-auto-wt",
        )
        agent_run_manager.start(run.id)

        async def _ok2() -> str:
            return "ok"

        done_task: asyncio.Task[str] = asyncio.ensure_future(_ok2())
        await done_task

        agent = _make_autonomous_agent(
            registry, run_id=run.id, session_id="sess-auto-wt", task=done_task
        )

        cleaned = await mon.check_dead_agents()

        assert cleaned == 1
        mock_coordinator.release_session_worktrees.assert_called_once_with(agent.session_id)

    @pytest.mark.asyncio
    async def test_releases_clones_on_failed_autonomous(
        self,
        registry: RunningAgentRegistry,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Clones are released when a failed autonomous agent is cleaned up."""
        mock_clone_storage = MagicMock()
        mock_clone_storage.release = MagicMock()  # Sync method, called via to_thread
        mon = AgentLifecycleMonitor(
            agent_registry=registry,
            agent_run_manager=agent_run_manager,
            clone_storage=mock_clone_storage,
        )

        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-auto-clone",
        )
        agent_run_manager.start(run.id)

        async def _failing() -> str:
            raise ValueError("boom")

        failed_task: asyncio.Task[str] = asyncio.ensure_future(_failing())
        try:
            await failed_task
        except ValueError:
            pass

        agent = RunningAgent(
            run_id=run.id,
            session_id="sess-auto-clone",
            parent_session_id="sess-parent",
            mode="autonomous",
            started_at=datetime.now(UTC),
            task=failed_task,
            clone_id="clone-123",
        )
        registry.add(agent)

        cleaned = await mon.check_dead_agents()

        assert cleaned == 1
        mock_clone_storage.release.assert_called_once_with("clone-123")


class TestCleanupOrphanedDbRuns:
    """Tests for cleanup_orphaned_db_runs (post-restart cleanup)."""

    @pytest.mark.asyncio
    async def test_marks_orphaned_runs_as_failed(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Running DB records with no registry entry are marked as failed."""
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-orphan",
        )
        agent_run_manager.start(run.id)

        # Registry is empty (simulates daemon restart)
        cleaned = await monitor.cleanup_orphaned_db_runs()

        assert cleaned == 1
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"
        assert "Orphaned" in (updated.error or "")

    @pytest.mark.asyncio
    async def test_skips_tracked_runs(
        self,
        monitor: AgentLifecycleMonitor,
        registry: RunningAgentRegistry,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Running DB records that ARE in the registry are left alone."""
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id="run-tracked",
        )
        agent_run_manager.start(run.id)
        _make_terminal_agent(registry, run_id=run.id)

        cleaned = await monitor.cleanup_orphaned_db_runs()

        assert cleaned == 0
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_no_running_runs_returns_zero(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """Returns 0 when there are no running DB records."""
        cleaned = await monitor.cleanup_orphaned_db_runs()
        assert cleaned == 0


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
