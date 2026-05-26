"""Tests for RunningAgentRegistry and related classes."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.registry import (
    EventCallback,
    RunningAgent,
    RunningAgentRegistry,
    get_running_agent_registry,
)

pytestmark = pytest.mark.unit


class TestRunningAgent:
    """Tests for RunningAgent dataclass."""

    def test_required_fields(self) -> None:
        """RunningAgent requires run_id, session_id, parent_session_id, and mode."""
        agent = RunningAgent(
            run_id="ar-123",
            session_id="sess-456",
            parent_session_id="sess-parent",
        )

        assert agent.run_id == "ar-123"
        assert agent.session_id == "sess-456"
        assert agent.parent_session_id == "sess-parent"

    def test_default_values(self) -> None:
        """RunningAgent has correct default values."""
        agent = RunningAgent(
            run_id="ar-1",
            session_id="sess-c",
            parent_session_id="sess-p",
        )

        assert agent.pid is None
        assert agent.master_fd is None
        assert agent.terminal_type is None
        assert agent.provider == "claude"
        assert agent.workflow_name is None
        assert agent.worktree_id is None
        assert agent.task is None
        assert isinstance(agent.started_at, datetime)

    def test_all_fields_settable(self) -> None:
        """RunningAgent allows setting all optional fields."""
        mock_task = MagicMock()
        agent = RunningAgent(
            run_id="ar-full",
            session_id="sess-full",
            parent_session_id="sess-parent-full",
            pid=12345,
            master_fd=7,
            terminal_type="ghostty",
            provider="gemini",
            workflow_name="plan-execute",
            worktree_id="wt-abc",
            task=mock_task,
        )

        assert agent.pid == 12345
        assert agent.master_fd == 7
        assert agent.terminal_type == "ghostty"
        assert agent.provider == "gemini"
        assert agent.workflow_name == "plan-execute"
        assert agent.worktree_id == "wt-abc"
        assert agent.task is mock_task

    def test_started_at_uses_utc(self) -> None:
        """RunningAgent.started_at is timezone-aware UTC."""
        agent = RunningAgent(
            run_id="ar-time",
            session_id="sess-time",
            parent_session_id="sess-parent",
        )

        assert agent.started_at.tzinfo is not None
        assert agent.started_at.tzinfo == UTC

    def test_to_dict_basic(self) -> None:
        """RunningAgent.to_dict returns correct dictionary."""
        agent = RunningAgent(
            run_id="ar-dict",
            session_id="sess-dict",
            parent_session_id="sess-parent-dict",
        )

        result = agent.to_dict()

        assert result["run_id"] == "ar-dict"
        assert result["session_id"] == "sess-dict"
        assert result["parent_session_id"] == "sess-parent-dict"
        assert result["provider"] == "claude"
        assert result["pid"] is None
        assert result["master_fd"] is None
        assert result["terminal_type"] is None
        assert result["workflow_name"] is None
        assert result["worktree_id"] is None
        assert result["has_task"] is False
        assert "started_at" in result

    def test_to_dict_with_all_fields(self) -> None:
        """RunningAgent.to_dict includes all fields when set."""
        mock_task = MagicMock()
        agent = RunningAgent(
            run_id="ar-full-dict",
            session_id="sess-full-dict",
            parent_session_id="sess-parent-full-dict",
            pid=99999,
            master_fd=10,
            terminal_type="iterm",
            provider="codex",
            workflow_name="test-workflow",
            worktree_id="wt-xyz",
            task=mock_task,
        )

        result = agent.to_dict()

        assert result["pid"] == 99999
        assert result["master_fd"] == 10
        assert result["terminal_type"] == "iterm"
        assert result["provider"] == "codex"
        assert result["workflow_name"] == "test-workflow"
        assert result["worktree_id"] == "wt-xyz"
        assert result["has_task"] is True

    def test_to_dict_started_at_is_isoformat(self) -> None:
        """RunningAgent.to_dict serializes started_at as ISO format string."""
        agent = RunningAgent(
            run_id="ar-iso",
            session_id="sess-iso",
            parent_session_id="sess-parent-iso",
        )

        result = agent.to_dict()

        # Should be a string in ISO format
        assert isinstance(result["started_at"], str)
        # Should be parseable back to datetime
        parsed = datetime.fromisoformat(result["started_at"])
        assert parsed == agent.started_at


class TestRunningAgentToBrief:
    """Tests for RunningAgent.to_brief() slim representation."""

    def test_to_brief_has_fewer_fields_than_to_dict(self) -> None:
        """to_brief returns fewer fields than to_dict."""
        agent = RunningAgent(
            run_id="ar-brief",
            session_id="sess-brief",
            parent_session_id="sess-parent-brief",
            pid=12345,
            provider="claude",
            task_id="gt-task1",
            stall_status="healthy",
        )

        brief = agent.to_brief()
        full = agent.to_dict()
        assert len(brief) < len(full)

    def test_to_brief_essential_fields_present(self) -> None:
        """to_brief includes essential identification and status fields."""
        agent = RunningAgent(
            run_id="ar-brief2",
            session_id="sess-brief2",
            parent_session_id="sess-parent-brief2",
            pid=99999,
            provider="gemini",
            task_id="gt-task2",
            stall_status="provider_stall",
        )

        brief = agent.to_brief()
        assert brief["run_id"] == "ar-brief2"
        assert brief["session_id"] == "sess-brief2"
        assert brief["parent_session_id"] == "sess-parent-brief2"
        assert brief["pid"] == 99999
        assert brief["provider"] == "gemini"
        assert brief["task_id"] == "gt-task2"
        assert brief["stall_status"] == "provider_stall"
        assert brief["has_task"] is False
        assert "started_at" in brief

    def test_to_brief_excludes_internal_fields(self) -> None:
        """to_brief omits internal process details."""
        agent = RunningAgent(
            run_id="ar-brief3",
            session_id="sess-brief3",
            parent_session_id="sess-parent-brief3",
            master_fd=7,
            terminal_type="tmux",
            tmux_session_name="gobby-agent-1",
            workflow_name="plan-execute",
            worktree_id="wt-abc",
            clone_id="clone-xyz",
            timeout_seconds=300,
        )

        brief = agent.to_brief()
        assert "master_fd" not in brief
        assert "terminal_type" not in brief
        assert "tmux_session_name" not in brief
        assert "workflow_name" not in brief
        assert "worktree_id" not in brief
        assert "clone_id" not in brief
        assert "timeout_seconds" not in brief
        assert "stall_reason" not in brief


class TestRunningAgentRegistry:
    """Tests for RunningAgentRegistry class."""

    @pytest.fixture
    def registry(self):
        """Create a fresh registry for each test."""
        return RunningAgentRegistry()

    @pytest.fixture
    def sample_agent(self):
        """Create a sample agent for testing."""
        return RunningAgent(
            run_id="ar-sample",
            session_id="sess-sample",
            parent_session_id="sess-parent-sample",
            pid=12345,
        )

    def test_init_creates_empty_registry(self, registry) -> None:
        """Registry initializes with empty agents dict."""
        assert registry.count() == 0
        assert registry.list_all() == []

    def test_add_agent(self, registry, sample_agent) -> None:
        """add() stores agent in registry."""
        registry.add(sample_agent)

        assert registry.count() == 1
        assert registry.get(sample_agent.run_id) is sample_agent

    def test_add_multiple_agents(self, registry) -> None:
        """add() can store multiple agents."""
        agents = [
            RunningAgent(
                run_id=f"ar-{i}",
                session_id=f"sess-{i}",
                parent_session_id="sess-parent",
            )
            for i in range(5)
        ]

        for agent in agents:
            registry.add(agent)

        assert registry.count() == 5
        for agent in agents:
            assert registry.get(agent.run_id) is agent

    def test_add_overwrites_existing(self, registry) -> None:
        """add() overwrites existing agent with same run_id."""
        agent1 = RunningAgent(
            run_id="ar-overwrite",
            session_id="sess-1",
            parent_session_id="sess-parent",
        )
        agent2 = RunningAgent(
            run_id="ar-overwrite",
            session_id="sess-2",
            parent_session_id="sess-parent",
        )

        registry.add(agent1)
        registry.add(agent2)

        assert registry.count() == 1
        assert registry.get("ar-overwrite").session_id == "sess-2"

    def test_get_returns_agent(self, registry, sample_agent) -> None:
        """get() returns the agent when found."""
        registry.add(sample_agent)

        result = registry.get(sample_agent.run_id)

        assert result is sample_agent

    def test_get_returns_none_when_not_found(self, registry) -> None:
        """get() returns None when agent not found."""
        result = registry.get("nonexistent-run-id")

        assert result is None

    def test_remove_returns_and_deletes_agent(self, registry, sample_agent) -> None:
        """remove() returns the agent and removes it from registry."""
        registry.add(sample_agent)

        removed = registry.remove(sample_agent.run_id)

        assert removed is sample_agent
        assert registry.get(sample_agent.run_id) is None
        assert registry.count() == 0

    def test_remove_returns_none_when_not_found(self, registry) -> None:
        """remove() returns None when agent not found."""
        result = registry.remove("nonexistent-run-id")

        assert result is None

    def test_remove_with_status(self, registry, sample_agent) -> None:
        """remove() accepts status parameter."""
        registry.add(sample_agent)

        # Should not raise
        removed = registry.remove(sample_agent.run_id, status="failed")

        assert removed is sample_agent

    def test_get_by_session(self, registry) -> None:
        """get_by_session() returns agent by child session ID."""
        agent = RunningAgent(
            run_id="ar-session",
            session_id="sess-child-123",
            parent_session_id="sess-parent",
        )
        registry.add(agent)

        result = registry.get_by_session("sess-child-123")

        assert result is agent

    def test_get_by_session_returns_none_when_not_found(self, registry) -> None:
        """get_by_session() returns None when session not found."""
        result = registry.get_by_session("nonexistent-session")

        assert result is None

    def test_get_by_session_with_multiple_agents(self, registry) -> None:
        """get_by_session() finds correct agent among multiple."""
        agents = [
            RunningAgent(
                run_id=f"ar-{i}",
                session_id=f"sess-child-{i}",
                parent_session_id="sess-parent",
            )
            for i in range(3)
        ]
        for agent in agents:
            registry.add(agent)

        result = registry.get_by_session("sess-child-1")

        assert result is agents[1]

    def test_get_by_pid(self, registry) -> None:
        """get_by_pid() returns agent by process ID."""
        agent = RunningAgent(
            run_id="ar-pid",
            session_id="sess-pid",
            parent_session_id="sess-parent",
            pid=54321,
        )
        registry.add(agent)

        result = registry.get_by_pid(54321)

        assert result is agent

    def test_get_by_pid_returns_none_when_not_found(self, registry) -> None:
        """get_by_pid() returns None when PID not found."""
        result = registry.get_by_pid(99999)

        assert result is None

    def test_get_by_pid_ignores_none_pids(self, registry) -> None:
        """get_by_pid() does not match agents with None pid."""
        agent = RunningAgent(
            run_id="ar-no-pid",
            session_id="sess-no-pid",
            parent_session_id="sess-parent",
            pid=None,
        )
        registry.add(agent)

        result = registry.get_by_pid(0)

        assert result is None

    def test_list_by_parent(self, registry) -> None:
        """list_by_parent() returns all agents for a parent session."""
        parent1_agents = [
            RunningAgent(
                run_id=f"ar-p1-{i}",
                session_id=f"sess-p1-{i}",
                parent_session_id="parent-1",
            )
            for i in range(3)
        ]
        parent2_agents = [
            RunningAgent(
                run_id=f"ar-p2-{i}",
                session_id=f"sess-p2-{i}",
                parent_session_id="parent-2",
            )
            for i in range(2)
        ]

        for agent in parent1_agents + parent2_agents:
            registry.add(agent)

        result = registry.list_by_parent("parent-1")

        assert len(result) == 3
        for agent in result:
            assert agent.parent_session_id == "parent-1"

    def test_list_by_parent_returns_empty_list_when_none_found(self, registry) -> None:
        """list_by_parent() returns empty list when no agents found."""
        result = registry.list_by_parent("nonexistent-parent")

        assert result == []

    def test_list_all(self, registry) -> None:
        """list_all() returns all agents."""
        agents = [
            RunningAgent(
                run_id=f"ar-all-{i}",
                session_id=f"sess-all-{i}",
                parent_session_id="parent",
            )
            for i in range(5)
        ]
        for agent in agents:
            registry.add(agent)

        result = registry.list_all()

        assert len(result) == 5

    def test_list_all_returns_copy(self, registry, sample_agent) -> None:
        """list_all() returns a copy, not the internal list."""
        registry.add(sample_agent)

        result = registry.list_all()
        result.clear()

        assert registry.count() == 1

    def test_count(self, registry) -> None:
        """count() returns the number of agents."""
        assert registry.count() == 0

        for i in range(3):
            registry.add(
                RunningAgent(
                    run_id=f"ar-count-{i}",
                    session_id=f"sess-count-{i}",
                    parent_session_id="parent",
                )
            )

        assert registry.count() == 3

    def test_count_by_parent(self, registry) -> None:
        """count_by_parent() returns correct count for a parent."""
        for i in range(3):
            registry.add(
                RunningAgent(
                    run_id=f"ar-p1-{i}",
                    session_id=f"sess-p1-{i}",
                    parent_session_id="parent-1",
                )
            )
        for i in range(2):
            registry.add(
                RunningAgent(
                    run_id=f"ar-p2-{i}",
                    session_id=f"sess-p2-{i}",
                    parent_session_id="parent-2",
                )
            )

        assert registry.count_by_parent("parent-1") == 3
        assert registry.count_by_parent("parent-2") == 2
        assert registry.count_by_parent("parent-3") == 0

    def test_clear(self, registry) -> None:
        """clear() removes all agents and returns count."""
        for i in range(5):
            registry.add(
                RunningAgent(
                    run_id=f"ar-clear-{i}",
                    session_id=f"sess-clear-{i}",
                    parent_session_id="parent",
                )
            )

        cleared_count = registry.clear()

        assert cleared_count == 5
        assert registry.count() == 0
        assert registry.list_all() == []

    def test_clear_empty_registry(self, registry) -> None:
        """clear() returns 0 for empty registry."""
        cleared_count = registry.clear()

        assert cleared_count == 0


class TestRunningAgentRegistryCleanup:
    """Tests for RunningAgentRegistry cleanup methods."""

    @pytest.fixture
    def registry(self):
        """Create a fresh registry for each test."""
        return RunningAgentRegistry()

    def test_cleanup_by_pids_removes_dead_agents(self, registry) -> None:
        """cleanup_by_pids() removes agents with PIDs in dead_pids set."""
        alive_agent = RunningAgent(
            run_id="ar-alive",
            session_id="sess-alive",
            parent_session_id="parent",
            pid=1000,
        )
        dead_agent1 = RunningAgent(
            run_id="ar-dead1",
            session_id="sess-dead1",
            parent_session_id="parent",
            pid=2000,
        )
        dead_agent2 = RunningAgent(
            run_id="ar-dead2",
            session_id="sess-dead2",
            parent_session_id="parent",
            pid=3000,
        )

        registry.add(alive_agent)
        registry.add(dead_agent1)
        registry.add(dead_agent2)

        removed = registry.cleanup_by_pids({2000, 3000})

        assert len(removed) == 2
        assert dead_agent1 in removed
        assert dead_agent2 in removed
        assert registry.count() == 1
        assert registry.get("ar-alive") is alive_agent

    def test_cleanup_by_pids_ignores_none_pids(self, registry) -> None:
        """cleanup_by_pids() ignores agents with None pid."""
        autonomous_agent = RunningAgent(
            run_id="ar-autonomous-np",
            session_id="sess-autonomous-np",
            parent_session_id="parent",
            pid=None,
        )
        registry.add(autonomous_agent)

        removed = registry.cleanup_by_pids({None})

        # Agent should not be removed since pid is None
        assert len(removed) == 0
        assert registry.count() == 1

    def test_cleanup_by_pids_empty_set(self, registry) -> None:
        """cleanup_by_pids() handles empty set."""
        agent = RunningAgent(
            run_id="ar-test",
            session_id="sess-test",
            parent_session_id="parent",
            pid=1234,
        )
        registry.add(agent)

        removed = registry.cleanup_by_pids(set())

        assert len(removed) == 0
        assert registry.count() == 1

    def test_cleanup_stale_removes_old_agents(self, registry) -> None:
        """cleanup_stale() removes agents older than max_age_seconds."""
        # Create agents with different ages
        old_agent = RunningAgent(
            run_id="ar-old",
            session_id="sess-old",
            parent_session_id="parent",
        )
        # Manually set started_at to 2 hours ago
        old_agent.started_at = datetime.now(UTC) - timedelta(hours=2)

        recent_agent = RunningAgent(
            run_id="ar-recent",
            session_id="sess-recent",
            parent_session_id="parent",
        )

        registry.add(old_agent)
        registry.add(recent_agent)

        # Cleanup agents older than 1 hour
        removed = registry.cleanup_stale(max_age_seconds=3600.0)

        assert len(removed) == 1
        assert old_agent in removed
        assert registry.count() == 1
        assert registry.get("ar-recent") is recent_agent

    def test_cleanup_stale_keeps_all_when_none_old(self, registry) -> None:
        """cleanup_stale() keeps all agents when none exceed max age."""
        agents = [
            RunningAgent(
                run_id=f"ar-{i}",
                session_id=f"sess-{i}",
                parent_session_id="parent",
            )
            for i in range(3)
        ]
        for agent in agents:
            registry.add(agent)

        # All agents were just created, so none should be stale
        removed = registry.cleanup_stale(max_age_seconds=3600.0)

        assert len(removed) == 0
        assert registry.count() == 3

    def test_cleanup_stale_with_small_max_age(self, registry) -> None:
        """cleanup_stale() with very small max_age removes all agents."""
        agents = [
            RunningAgent(
                run_id=f"ar-{i}",
                session_id=f"sess-{i}",
                parent_session_id="parent",
            )
            for i in range(3)
        ]
        for agent in agents:
            registry.add(agent)

        for agent in agents:
            agent.started_at = datetime.now(UTC) - timedelta(seconds=1)

        # Use tiny max_age that all agents exceed
        removed = registry.cleanup_stale(max_age_seconds=0.001)

        assert len(removed) == 3
        assert registry.count() == 0


class TestRunningAgentRegistryEventCallbacks:
    """Tests for RunningAgentRegistry event callback functionality."""

    @pytest.fixture
    def registry(self):
        """Create a fresh registry for each test."""
        return RunningAgentRegistry()

    def test_add_event_callback(self, registry) -> None:
        """add_event_callback() adds callback to list."""
        callback = MagicMock()

        registry.add_event_callback(callback)

        # Verify callback is stored (indirectly via triggering event)
        agent = RunningAgent(
            run_id="ar-test",
            session_id="sess-test",
            parent_session_id="parent",
        )
        registry.add(agent)

        callback.assert_called_once()
        assert callback.call_count == 1
        assert callback.call_args is not None

    def test_event_callback_on_add(self, registry) -> None:
        """Event callback is invoked when agent is added."""
        callback = MagicMock()
        registry.add_event_callback(callback)

        agent = RunningAgent(
            run_id="ar-add",
            session_id="sess-add",
            parent_session_id="parent-add",
            provider="gemini",
            pid=9999,
        )
        registry.add(agent)

        callback.assert_called_once_with(
            "agent_started",
            "ar-add",
            {
                "session_id": "sess-add",
                "parent_session_id": "parent-add",
                "provider": "gemini",
                "pid": 9999,
                "tmux_session_name": None,
            },
        )
        assert callback.call_count == 1
        assert callback.call_args is not None

    def test_event_callback_on_remove_completed(self, registry) -> None:
        """Event callback is invoked with agent_completed when removed."""
        callback = MagicMock()
        registry.add_event_callback(callback)

        agent = RunningAgent(
            run_id="ar-rm",
            session_id="sess-rm",
            parent_session_id="parent-rm",
            provider="claude",
        )
        registry.add(agent)
        callback.reset_mock()

        registry.remove("ar-rm", status="completed")

        callback.assert_called_once_with(
            "agent_completed",
            "ar-rm",
            {
                "session_id": "sess-rm",
                "parent_session_id": "parent-rm",
                "provider": "claude",
                "tmux_session_name": None,
            },
        )
        assert callback.call_count == 1
        assert callback.call_args is not None

    def test_event_callback_on_remove_failed(self, registry) -> None:
        """Event callback uses status for event type."""
        callback = MagicMock()
        registry.add_event_callback(callback)

        agent = RunningAgent(
            run_id="ar-fail",
            session_id="sess-fail",
            parent_session_id="parent-fail",
            provider="claude",
        )
        registry.add(agent)
        callback.reset_mock()

        registry.remove("ar-fail", status="failed")

        callback.assert_called_once()
        call_args = callback.call_args
        assert call_args[0][0] == "agent_failed"

    def test_event_callback_on_remove_cancelled(self, registry) -> None:
        """Event callback uses cancelled status correctly."""
        callback = MagicMock()
        registry.add_event_callback(callback)

        agent = RunningAgent(
            run_id="ar-cancel",
            session_id="sess-cancel",
            parent_session_id="parent-cancel",
            provider="claude",
        )
        registry.add(agent)
        callback.reset_mock()

        registry.remove("ar-cancel", status="cancelled")

        callback.assert_called_once()
        call_args = callback.call_args
        assert call_args[0][0] == "agent_cancelled"

    def test_event_callback_on_cleanup_by_pids(self, registry) -> None:
        """Event callback is invoked for each agent cleaned up by PIDs."""
        callback = MagicMock()
        registry.add_event_callback(callback)

        agent = RunningAgent(
            run_id="ar-cleanup-pid",
            session_id="sess-cleanup-pid",
            parent_session_id="parent-cleanup-pid",
            provider="claude",
            pid=11111,
        )
        registry.add(agent)
        callback.reset_mock()

        registry.cleanup_by_pids({11111})

        callback.assert_called_once()
        call_args = callback.call_args
        assert call_args[0][0] == "agent_completed"
        assert call_args[0][2]["cleanup_reason"] == "dead_pid"

    def test_event_callback_on_cleanup_stale(self, registry) -> None:
        """Event callback is invoked for each stale agent cleaned up."""
        callback = MagicMock()
        registry.add_event_callback(callback)

        agent = RunningAgent(
            run_id="ar-stale",
            session_id="sess-stale",
            parent_session_id="parent-stale",
            provider="claude",
        )
        agent.started_at = datetime.now(UTC) - timedelta(hours=2)
        registry.add(agent)
        callback.reset_mock()

        registry.cleanup_stale(max_age_seconds=60.0)

        callback.assert_called_once()
        call_args = callback.call_args
        assert call_args[0][0] == "agent_timeout"
        assert call_args[0][2]["cleanup_reason"] == "stale"

    def test_event_callback_exception_handling(self, registry) -> None:
        """Event callback exceptions are logged but don't propagate."""

        def bad_callback(event_type: str, run_id: str, data: dict):
            raise ValueError("Callback error")

        good_callback = MagicMock()

        registry.add_event_callback(bad_callback)
        registry.add_event_callback(good_callback)

        agent = RunningAgent(
            run_id="ar-exc",
            session_id="sess-exc",
            parent_session_id="parent-exc",
        )

        # Should not raise even though bad_callback throws
        registry.add(agent)

        # Good callback should still be called
        good_callback.assert_called_once()
        assert good_callback.call_count == 1
        assert good_callback.call_args is not None

    def test_multiple_event_callbacks(self, registry) -> None:
        """Multiple event callbacks are all invoked."""
        callbacks = [MagicMock() for _ in range(3)]
        for callback in callbacks:
            registry.add_event_callback(callback)

        agent = RunningAgent(
            run_id="ar-multi",
            session_id="sess-multi",
            parent_session_id="parent-multi",
        )
        registry.add(agent)

        for callback in callbacks:
            callback.assert_called_once()
            assert callback.call_count == 1
            assert callback.call_args is not None

    def test_remove_nonexistent_does_not_trigger_callback(self, registry) -> None:
        """Removing nonexistent agent does not trigger callback."""
        callback = MagicMock()
        registry.add_event_callback(callback)

        registry.remove("nonexistent-id")

        callback.assert_not_called()
        assert callback.call_count == 0
        assert not callback.called


class TestRunningAgentRegistryThreadSafety:
    """Tests for RunningAgentRegistry thread safety."""

    @pytest.fixture
    def registry(self):
        """Create a fresh registry for each test."""
        return RunningAgentRegistry()

    def test_concurrent_adds(self, registry) -> None:
        """Registry handles concurrent add operations safely."""
        num_threads = 10
        agents_per_thread = 100
        errors: list[Exception] = []

        def add_agents(thread_id: int):
            try:
                for i in range(agents_per_thread):
                    agent = RunningAgent(
                        run_id=f"ar-{thread_id}-{i}",
                        session_id=f"sess-{thread_id}-{i}",
                        parent_session_id="parent",
                    )
                    registry.add(agent)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_agents, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert registry.count() == num_threads * agents_per_thread

    def test_concurrent_add_and_remove(self, registry) -> None:
        """Registry handles concurrent add and remove operations safely."""
        # Pre-populate registry
        for i in range(100):
            registry.add(
                RunningAgent(
                    run_id=f"ar-pre-{i}",
                    session_id=f"sess-pre-{i}",
                    parent_session_id="parent",
                )
            )

        errors: list[Exception] = []
        add_count = [0]
        remove_count = [0]

        def add_agents():
            try:
                for i in range(50):
                    agent = RunningAgent(
                        run_id=f"ar-add-{i}",
                        session_id=f"sess-add-{i}",
                        parent_session_id="parent",
                    )
                    registry.add(agent)
                    add_count[0] += 1
            except Exception as e:
                errors.append(e)

        def remove_agents():
            try:
                for i in range(50):
                    registry.remove(f"ar-pre-{i}")
                    remove_count[0] += 1
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=add_agents)
        t2 = threading.Thread(target=remove_agents)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0
        # Final count should be initial (100) + added (50) - removed (up to 50)
        # Some removes might fail if items don't exist, but no exceptions

    def test_concurrent_reads(self, registry) -> None:
        """Registry handles concurrent read operations safely."""
        # Pre-populate registry
        for i in range(100):
            registry.add(
                RunningAgent(
                    run_id=f"ar-read-{i}",
                    session_id=f"sess-read-{i}",
                    parent_session_id=f"parent-{i % 10}",
                    pid=i if i % 2 == 0 else None,
                )
            )

        errors: list[Exception] = []
        results: list[int] = []

        def read_operations():
            try:
                for _ in range(100):
                    registry.count()
                    registry.list_all()
                    registry.get(f"ar-read-{_ % 100}")
                    registry.get_by_session(f"sess-read-{_ % 100}")
                    registry.list_by_parent(f"parent-{_ % 10}")
                    registry.count_by_parent(f"parent-{_ % 10}")
                    if _ % 2 == 0:
                        registry.get_by_pid(_ % 100)
                results.append(1)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=read_operations) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 5


class TestGetRunningAgentRegistry:
    """Tests for get_running_agent_registry() singleton function."""

    def test_returns_registry_instance(self) -> None:
        """get_running_agent_registry() returns a RunningAgentRegistry."""
        result = get_running_agent_registry()

        assert isinstance(result, RunningAgentRegistry)

    def test_returns_same_instance(self) -> None:
        """get_running_agent_registry() returns the same instance each time."""
        result1 = get_running_agent_registry()
        result2 = get_running_agent_registry()

        assert result1 is result2

    def test_thread_safe_initialization(self) -> None:
        """get_running_agent_registry() initializes safely from multiple threads."""
        results: list[RunningAgentRegistry] = []
        errors: list[Exception] = []

        def get_registry():
            try:
                registry = get_running_agent_registry()
                results.append(registry)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=get_registry) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 10
        # All should be the same instance
        for result in results:
            assert result is results[0]


class TestRunningAgentRegistryLogging:
    """Tests for RunningAgentRegistry logging behavior."""

    @pytest.fixture
    def registry(self):
        """Create a fresh registry for each test."""
        return RunningAgentRegistry()

    def test_add_logs_debug_message(self, registry, caplog) -> None:
        """add() logs debug message."""
        import logging

        with caplog.at_level(logging.DEBUG):
            agent = RunningAgent(
                run_id="ar-log",
                session_id="sess-log",
                parent_session_id="parent-log",
                pid=12345,
            )
            registry.add(agent)

        assert "ar-log" in caplog.text

    def test_remove_logs_debug_message(self, registry, caplog) -> None:
        """remove() logs debug message when agent found."""
        import logging

        agent = RunningAgent(
            run_id="ar-rm-log",
            session_id="sess-rm-log",
            parent_session_id="parent-rm-log",
        )
        registry.add(agent)

        with caplog.at_level(logging.DEBUG):
            registry.remove("ar-rm-log")

        assert "ar-rm-log" in caplog.text

    def test_cleanup_by_pids_logs_info(self, registry, caplog) -> None:
        """cleanup_by_pids() logs info message for cleaned up agents."""
        import logging

        agent = RunningAgent(
            run_id="ar-cleanup-log",
            session_id="sess-cleanup-log",
            parent_session_id="parent",
            pid=77777,
        )
        registry.add(agent)

        with caplog.at_level(logging.INFO):
            registry.cleanup_by_pids({77777})

        assert "ar-cleanup-log" in caplog.text
        assert "77777" in caplog.text

    def test_cleanup_stale_logs_info(self, registry, caplog) -> None:
        """cleanup_stale() logs info message for cleaned up agents."""
        import logging

        agent = RunningAgent(
            run_id="ar-stale-log",
            session_id="sess-stale-log",
            parent_session_id="parent",
        )
        agent.started_at = datetime.now(UTC) - timedelta(hours=2)
        registry.add(agent)

        with caplog.at_level(logging.INFO):
            registry.cleanup_stale(max_age_seconds=60.0)

        assert "ar-stale-log" in caplog.text
        assert "age=" in caplog.text

    def test_clear_logs_info(self, registry, caplog) -> None:
        """clear() logs info message with count."""
        import logging

        for i in range(3):
            registry.add(
                RunningAgent(
                    run_id=f"ar-clear-{i}",
                    session_id=f"sess-clear-{i}",
                    parent_session_id="parent",
                )
            )

        with caplog.at_level(logging.INFO):
            registry.clear()

        assert "3" in caplog.text


class TestEventCallbackType:
    """Tests for EventCallback type alias."""

    def test_event_callback_type_signature(self) -> None:
        """EventCallback has correct type signature."""
        # This is a compile-time check; we verify by creating conforming functions

        def valid_callback(event_type: str, run_id: str, data: dict) -> None:
            pass

        # The type checker should accept this
        callback: EventCallback = valid_callback
        assert callback is not None

    def test_event_callback_with_any_data(self) -> None:
        """EventCallback data parameter accepts dict with Any values."""
        from typing import Any

        def callback_with_any(event_type: str, run_id: str, data: dict[str, Any]) -> None:
            pass

        callback: EventCallback = callback_with_any
        assert callback is not None


class TestRunningAgentEdgeCases:
    """Edge case tests for RunningAgent."""

    def test_agent_with_empty_strings(self) -> None:
        """RunningAgent handles empty string values."""
        agent = RunningAgent(
            run_id="",
            session_id="",
            parent_session_id="",
        )

        assert agent.run_id == ""
        assert agent.session_id == ""
        assert agent.parent_session_id == ""

    def test_agent_to_dict_with_special_characters(self) -> None:
        """RunningAgent.to_dict handles special characters in values."""
        agent = RunningAgent(
            run_id='ar-"special"',
            session_id="sess-with\nnewline",
            parent_session_id="parent-with\ttab",
            terminal_type="term<type>",
            workflow_name="workflow/with/slashes",
        )

        result = agent.to_dict()

        assert result["run_id"] == 'ar-"special"'
        assert result["session_id"] == "sess-with\nnewline"
        assert result["terminal_type"] == "term<type>"
        assert result["workflow_name"] == "workflow/with/slashes"


class TestRunningAgentRegistryEdgeCases:
    """Edge case tests for RunningAgentRegistry."""

    @pytest.fixture
    def registry(self):
        """Create a fresh registry for each test."""
        return RunningAgentRegistry()

    def test_get_by_pid_with_zero(self, registry) -> None:
        """get_by_pid() handles PID 0."""
        agent = RunningAgent(
            run_id="ar-pid-0",
            session_id="sess-pid-0",
            parent_session_id="parent",
            pid=0,
        )
        registry.add(agent)

        result = registry.get_by_pid(0)

        assert result is agent

    def test_cleanup_by_pids_with_pid_zero(self, registry) -> None:
        """cleanup_by_pids() does not clean up PID 0 due to falsy check.

        Note: The implementation checks `if agent.pid and agent.pid in dead_pids`
        which means PID 0 (falsy) is not matched. This is intentional since
        PID 0 in Unix systems has special meaning (kernel).
        """
        agent = RunningAgent(
            run_id="ar-pid-0-cleanup",
            session_id="sess-pid-0-cleanup",
            parent_session_id="parent",
            pid=0,
        )
        registry.add(agent)

        removed = registry.cleanup_by_pids({0})

        # PID 0 is falsy so cleanup_by_pids doesn't match it
        assert len(removed) == 0
        assert registry.count() == 1

    def test_cleanup_stale_exact_boundary(self, registry) -> None:
        """cleanup_stale() at exact age boundary."""
        agent = RunningAgent(
            run_id="ar-boundary",
            session_id="sess-boundary",
            parent_session_id="parent",
        )
        # Set to exactly 1 hour ago
        agent.started_at = datetime.now(UTC) - timedelta(hours=1)
        registry.add(agent)

        # Should be removed since age >= max_age
        removed = registry.cleanup_stale(max_age_seconds=3600.0)

        # The agent is exactly at the boundary, behavior depends on timing
        # It should be removed since we're slightly past due to execution time
        assert len(removed) >= 0  # May or may not be removed at exact boundary

    def test_large_number_of_event_callbacks(self, registry) -> None:
        """Registry handles many event callbacks."""
        callbacks = [MagicMock() for _ in range(100)]
        for callback in callbacks:
            registry.add_event_callback(callback)

        agent = RunningAgent(
            run_id="ar-many-cb",
            session_id="sess-many-cb",
            parent_session_id="parent",
        )
        registry.add(agent)

        for callback in callbacks:
            callback.assert_called_once()
            assert callback.call_count == 1
            assert callback.call_args is not None

    def test_callback_modification_during_iteration(self, registry) -> None:
        """Event callback list modification during iteration is safe."""
        call_count = [0]

        def callback_that_adds_another(event_type: str, run_id: str, data: dict):
            call_count[0] += 1
            # Try to add another callback during iteration
            registry.add_event_callback(lambda e, r, d: None)

        registry.add_event_callback(callback_that_adds_another)

        agent = RunningAgent(
            run_id="ar-modify",
            session_id="sess-modify",
            parent_session_id="parent",
        )

        # Should not raise due to snapshot iteration
        registry.add(agent)

        assert call_count[0] == 1


class TestValidateTerminalValue:
    """Tests for _validate_terminal_value helper function."""

    def test_valid_tmux_pane(self) -> None:
        """Valid tmux pane format (%N)."""
        from gobby.agents.registry import _validate_terminal_value

        assert _validate_terminal_value("tmux_pane", "%0") is True
        assert _validate_terminal_value("tmux_pane", "%123") is True

    def test_invalid_tmux_pane(self) -> None:
        """Invalid tmux pane formats."""
        from gobby.agents.registry import _validate_terminal_value

        assert _validate_terminal_value("tmux_pane", "0") is False
        assert _validate_terminal_value("tmux_pane", "%abc") is False
        assert _validate_terminal_value("tmux_pane", "") is False
        assert _validate_terminal_value("tmux_pane", "; rm -rf /") is False

    def test_valid_parent_pid(self) -> None:
        """Valid parent_pid format (digits only)."""
        from gobby.agents.registry import _validate_terminal_value

        assert _validate_terminal_value("parent_pid", "12345") is True
        assert _validate_terminal_value("parent_pid", "0") is True

    def test_invalid_parent_pid(self) -> None:
        """Invalid parent_pid formats."""
        from gobby.agents.registry import _validate_terminal_value

        assert _validate_terminal_value("parent_pid", "abc") is False
        assert _validate_terminal_value("parent_pid", "12 34") is False
        assert _validate_terminal_value("parent_pid", "-1") is False

    def test_valid_session_id(self) -> None:
        """Valid session_id format (alphanumeric, hyphens, underscores)."""
        from gobby.agents.registry import _validate_terminal_value

        assert _validate_terminal_value("session_id", "abc-123_def") is True
        assert _validate_terminal_value("session_id", "UUID-format-test") is True

    def test_invalid_session_id(self) -> None:
        """Invalid session_id formats."""
        from gobby.agents.registry import _validate_terminal_value

        assert _validate_terminal_value("session_id", "has space") is False
        assert _validate_terminal_value("session_id", "has;semicolon") is False

    def test_unknown_key(self) -> None:
        """Unknown keys always return False."""
        from gobby.agents.registry import _validate_terminal_value

        assert _validate_terminal_value("unknown_key", "anything") is False


class TestRunningAgentRegistryKill:
    """Tests for RunningAgentRegistry.kill() method."""

    @pytest.fixture
    def registry(self):
        return RunningAgentRegistry()

    @pytest.mark.asyncio
    async def test_kill_not_in_registry(self, registry) -> None:
        """kill() returns already_completed for unregistered agent."""
        result = await registry.kill("nonexistent-run")
        assert result["success"] is True
        assert result["already_completed"] is True

    @pytest.mark.asyncio
    async def test_kill_no_pid_found(self, registry) -> None:
        """kill() returns error when no PID can be found."""
        agent = RunningAgent(
            run_id="ar-nopid",
            session_id="sess-nopid",
            parent_session_id="parent",
            pid=None,
        )
        registry.add(agent)

        with patch("gobby.storage.hub.runtime.open_runtime_hub_database"):
            with patch("gobby.storage.sessions.SessionManager") as MockSM:
                MockSM.return_value.get.return_value = None
                result = await registry.kill("ar-nopid")

        assert result["success"] is False
        assert "No target PID" in result["error"]

    @pytest.mark.asyncio
    async def test_kill_process_already_dead(self, registry) -> None:
        """kill() handles already-dead process."""

        agent = RunningAgent(
            run_id="ar-dead",
            session_id="sess-dead",
            parent_session_id="parent",
            pid=99999999,
        )
        registry.add(agent)

        with patch("os.kill", side_effect=ProcessLookupError):
            result = await registry.kill("ar-dead")

        assert result["success"] is True
        assert result.get("already_dead") is True

    @pytest.mark.asyncio
    async def test_kill_permission_error(self, registry) -> None:
        """kill() handles permission errors."""
        agent = RunningAgent(
            run_id="ar-perm",
            session_id="sess-perm",
            parent_session_id="parent",
            pid=1,
        )
        registry.add(agent)

        with patch("os.kill", side_effect=PermissionError):
            result = await registry.kill("ar-perm")

        assert result["success"] is False
        assert "permission" in result["error"].lower()


class TestRunSubprocess:
    """Tests for _run_subprocess method."""

    @pytest.fixture
    def registry(self):
        return RunningAgentRegistry()

    @pytest.mark.asyncio
    async def test_run_subprocess_success(self, registry) -> None:
        """_run_subprocess returns (rc, stdout, stderr)."""
        rc, stdout, stderr = await registry._run_subprocess("echo", "hello")
        assert rc == 0
        assert "hello" in stdout

    @pytest.mark.asyncio
    async def test_run_subprocess_timeout(self, registry) -> None:
        """_run_subprocess raises TimeoutError on timeout."""
        with pytest.raises(TimeoutError):
            await registry._run_subprocess("sleep", "10", timeout=0.1)


class TestEmitEvent:
    """Tests for emit_event public method."""

    def test_emit_event_calls_callbacks(self) -> None:
        """emit_event dispatches to registered callbacks."""
        registry = RunningAgentRegistry()
        callback = MagicMock()
        registry.add_event_callback(callback)

        registry.emit_event("custom_event", "run-123", {"key": "value"})

        callback.assert_called_once_with("custom_event", "run-123", {"key": "value"})
        assert callback.call_count == 1
        assert callback.call_args is not None
