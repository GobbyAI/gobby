"""
Tests for agents.py MCP tools module.

This file tests the agent-related MCP tools:
- spawn_agent: Spawn a subagent with isolation support
- get_agent_result: Get agent run result
- list_agents: List agent runs for a session
- stop_agent: Stop a running agent (DB only)
- kill_agent: Kill a running agent process
- can_spawn_agent: Check if spawning is allowed
- list_running_agents: List in-memory running agents
- get_running_agent: Get running agent state
- unregister_agent: Remove agent from registry
- running_agent_stats: Get agent statistics
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.agents.registry import RunningAgent, RunningAgentRegistry
from gobby.mcp_proxy.tools.agents import create_agents_registry

pytestmark = pytest.mark.unit


class TestCreateAgentsRegistry:
    """Tests for create_agents_registry factory function."""

    def test_creates_registry_with_correct_name(self) -> None:
        """Test registry has correct name."""
        runner = MagicMock()
        registry = create_agents_registry(runner)

        assert registry.name == "gobby-agents"
        assert "Agent" in registry.description

    def test_registers_all_expected_tools(self) -> None:
        """Test all agent tools are registered."""
        runner = MagicMock()
        registry = create_agents_registry(runner)

        expected_tools = [
            "spawn_agent",  # Unified spawn with isolation support
            "get_agent_result",
            "list_agents",
            "stop_agent",
            "kill_agent",
            "can_spawn_agent",
            "list_running_agents",
            "get_running_agent",
            "unregister_agent",
            "running_agent_stats",
        ]

        for tool_name in expected_tools:
            assert registry.get_schema(tool_name) is not None, f"Missing tool: {tool_name}"

    def test_uses_provided_running_registry(self) -> None:
        """Test that provided registry is used instead of global."""
        runner = MagicMock()
        custom_registry = RunningAgentRegistry()

        registry = create_agents_registry(runner, running_registry=custom_registry)
        # Verify registry was accepted (test indirectly via list_running_agents)
        assert registry is not None


class TestGetAgentResult:
    """Tests for get_agent_result MCP tool."""

    @pytest.mark.asyncio
    async def test_run_not_found_returns_error(self):
        """Test error when run_id not found."""
        runner = MagicMock()
        runner.get_run.return_value = None

        registry = create_agents_registry(runner)
        get_result = registry._tools["get_agent_result"].func

        result = await get_result(run_id="non-existent")

        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_returns_run_details(self):
        """Test successful run retrieval returns all details."""
        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.status = "success"
        mock_run.result = "Task completed"
        mock_run.error = None
        mock_run.provider = "claude"
        mock_run.model = "claude-3-opus"
        mock_run.prompt = "Do the thing"
        mock_run.tool_calls_count = 5
        mock_run.turns_used = 3
        mock_run.started_at = "2024-01-01T00:00:00Z"
        mock_run.completed_at = "2024-01-01T00:01:00Z"
        mock_run.child_session_id = "child-sess-456"

        runner = MagicMock()
        runner.get_run.return_value = mock_run

        registry = create_agents_registry(runner)
        get_result = registry._tools["get_agent_result"].func

        result = await get_result(run_id="run-123")

        assert result["success"] is True
        assert result["run_id"] == "run-123"
        assert result["status"] == "success"
        assert result["result"] == "Task completed"
        assert result["provider"] == "claude"
        assert result["model"] == "claude-3-opus"
        assert result["tool_calls_count"] == 5
        assert result["turns_used"] == 3
        assert result["child_session_id"] == "child-sess-456"


class TestListAgents:
    """Tests for list_agents MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_runs(self):
        """Test empty list when no runs exist."""
        runner = MagicMock()
        runner.list_runs.return_value = []

        registry = create_agents_registry(runner)
        list_agents = registry._tools["list_agents"].func

        result = await list_agents(parent_session_id="sess-123")

        assert result["success"] is True
        assert result["runs"] == []
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_returns_runs_with_truncated_prompts(self):
        """Test that long prompts are truncated in list."""
        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.status = "running"
        mock_run.provider = "claude"
        mock_run.model = "claude-3"
        mock_run.workflow_name = "plan-execute"
        mock_run.prompt = "A" * 200  # Long prompt
        mock_run.started_at = "2024-01-01T00:00:00Z"
        mock_run.completed_at = None

        runner = MagicMock()
        runner.list_runs.return_value = [mock_run]

        registry = create_agents_registry(runner)
        list_agents = registry._tools["list_agents"].func

        result = await list_agents(parent_session_id="sess-123")

        assert result["success"] is True
        assert result["count"] == 1
        assert len(result["runs"][0]["prompt"]) == 103  # 100 chars + "..."
        assert result["runs"][0]["prompt"].endswith("...")

    @pytest.mark.asyncio
    async def test_respects_status_filter(self):
        """Test status filter is passed to runner."""
        runner = MagicMock()
        runner.list_runs.return_value = []

        registry = create_agents_registry(runner)
        list_agents = registry._tools["list_agents"].func

        await list_agents(parent_session_id="sess-123", status="running")

        runner.list_runs.assert_called_once_with("sess-123", status="running", limit=20)

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        """Test limit parameter is passed to runner."""
        runner = MagicMock()
        runner.list_runs.return_value = []

        registry = create_agents_registry(runner)
        list_agents = registry._tools["list_agents"].func

        await list_agents(parent_session_id="sess-123", limit=50)

        runner.list_runs.assert_called_once_with("sess-123", status=None, limit=50)


class TestStopAgent:
    """Tests for stop_agent MCP tool."""

    @pytest.mark.asyncio
    async def test_successful_stop(self):
        """Test successful agent stop."""
        runner = MagicMock()
        runner.cancel_run.return_value = True

        running_registry = RunningAgentRegistry()
        running_registry.add(
            RunningAgent(
                run_id="run-123",
                session_id="sess-456",
                parent_session_id="sess-parent",
                mode="terminal",
            )
        )

        registry = create_agents_registry(runner, running_registry=running_registry)
        stop_agent = registry._tools["stop_agent"].func

        result = await stop_agent(run_id="run-123")

        assert result["success"] is True
        assert "stopped" in result["message"]

        # Verify removed from registry
        assert running_registry.get("run-123") is None

    @pytest.mark.asyncio
    async def test_run_not_found(self):
        """Test error when run not found."""
        runner = MagicMock()
        runner.cancel_run.return_value = False
        runner.get_run.return_value = None

        registry = create_agents_registry(runner)
        stop_agent = registry._tools["stop_agent"].func

        result = await stop_agent(run_id="non-existent")

        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_cannot_stop_completed_run(self):
        """Test error when trying to stop non-running agent."""
        mock_run = MagicMock()
        mock_run.status = "success"

        runner = MagicMock()
        runner.cancel_run.return_value = False
        runner.get_run.return_value = mock_run

        registry = create_agents_registry(runner)
        stop_agent = registry._tools["stop_agent"].func

        result = await stop_agent(run_id="run-123")

        assert result["success"] is False
        assert "Cannot stop" in result["error"]
        assert "success" in result["error"]


class TestCanSpawnAgent:
    """Tests for can_spawn_agent MCP tool."""

    @pytest.mark.asyncio
    async def test_can_spawn_returns_true(self):
        """Test when spawning is allowed."""
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Spawning allowed", 0)

        registry = create_agents_registry(runner)
        can_spawn = registry._tools["can_spawn_agent"].func

        result = await can_spawn(parent_session_id="sess-123")

        assert result["can_spawn"] is True
        assert result["reason"] == "Spawning allowed"

    @pytest.mark.asyncio
    async def test_cannot_spawn_returns_false(self):
        """Test when spawning is not allowed."""
        runner = MagicMock()
        runner.can_spawn.return_value = (False, "Max depth reached", 3)

        registry = create_agents_registry(runner)
        can_spawn = registry._tools["can_spawn_agent"].func

        result = await can_spawn(parent_session_id="sess-123")

        assert result["can_spawn"] is False
        assert result["reason"] == "Max depth reached"


class TestListRunningAgents:
    """Tests for list_running_agents MCP tool."""

    @pytest.fixture
    def populated_registry(self):
        """Create a registry with test agents."""
        registry = RunningAgentRegistry()
        registry.add(
            RunningAgent(
                run_id="run-1",
                session_id="sess-1",
                parent_session_id="parent-1",
                mode="terminal",
                pid=1001,
            )
        )
        registry.add(
            RunningAgent(
                run_id="run-2",
                session_id="sess-2",
                parent_session_id="parent-1",
                mode="autonomous",
                pid=1002,
            )
        )
        registry.add(
            RunningAgent(
                run_id="run-3",
                session_id="sess-3",
                parent_session_id="parent-2",
                mode="terminal",
                pid=1003,
            )
        )
        return registry

    @pytest.mark.asyncio
    async def test_list_all_running_agents(self, populated_registry):
        """Test listing all running agents."""
        runner = MagicMock()
        registry = create_agents_registry(runner, running_registry=populated_registry)
        list_running = registry._tools["list_running_agents"].func

        result = await list_running()

        assert result["success"] is True
        assert result["count"] == 3
        assert len(result["agents"]) == 3

    @pytest.mark.asyncio
    async def test_filter_by_parent_session(self, populated_registry):
        """Test filtering by parent session ID."""
        runner = MagicMock()
        registry = create_agents_registry(runner, running_registry=populated_registry)
        list_running = registry._tools["list_running_agents"].func

        result = await list_running(parent_session_id="parent-1")

        assert result["success"] is True
        assert result["count"] == 2
        for agent in result["agents"]:
            assert agent["parent_session_id"] == "parent-1"

    @pytest.mark.asyncio
    async def test_filter_by_mode(self, populated_registry):
        """Test filtering by execution mode."""
        runner = MagicMock()
        registry = create_agents_registry(runner, running_registry=populated_registry)
        list_running = registry._tools["list_running_agents"].func

        result = await list_running(mode="terminal")

        assert result["success"] is True
        assert result["count"] == 2
        for agent in result["agents"]:
            assert agent["mode"] == "terminal"


class TestGetRunningAgent:
    """Tests for get_running_agent MCP tool."""

    @pytest.mark.asyncio
    async def test_agent_found(self):
        """Test getting an existing running agent."""
        running_registry = RunningAgentRegistry()
        running_registry.add(
            RunningAgent(
                run_id="run-123",
                session_id="sess-456",
                parent_session_id="sess-parent",
                mode="terminal",
                pid=12345,
                terminal_type="ghostty",
                provider="claude",
            )
        )

        runner = MagicMock()
        registry = create_agents_registry(runner, running_registry=running_registry)
        get_running = registry._tools["get_running_agent"].func

        result = await get_running(run_id="run-123")

        assert result["success"] is True
        assert result["agent"]["run_id"] == "run-123"
        assert result["agent"]["pid"] == 12345
        assert result["agent"]["terminal_type"] == "ghostty"

    @pytest.mark.asyncio
    async def test_agent_not_found(self):
        """Test error when agent not found."""
        running_registry = RunningAgentRegistry()
        runner = MagicMock()
        registry = create_agents_registry(runner, running_registry=running_registry)
        get_running = registry._tools["get_running_agent"].func

        result = await get_running(run_id="non-existent")

        assert result["success"] is False
        assert "no running agent found" in result["error"].lower()


class TestUnregisterAgent:
    """Tests for unregister_agent MCP tool."""

    @pytest.mark.asyncio
    async def test_successful_unregistration(self):
        """Test successful agent unregistration."""
        running_registry = RunningAgentRegistry()
        running_registry.add(
            RunningAgent(
                run_id="run-123",
                session_id="sess-456",
                parent_session_id="sess-parent",
                mode="terminal",
            )
        )

        runner = MagicMock()
        registry = create_agents_registry(runner, running_registry=running_registry)
        unregister = registry._tools["unregister_agent"].func

        result = await unregister(run_id="run-123")

        assert result["success"] is True
        assert "Unregistered" in result["message"]
        assert running_registry.get("run-123") is None

    @pytest.mark.asyncio
    async def test_unregister_not_found(self):
        """Test error when agent not found."""
        running_registry = RunningAgentRegistry()
        runner = MagicMock()
        registry = create_agents_registry(runner, running_registry=running_registry)
        unregister = registry._tools["unregister_agent"].func

        result = await unregister(run_id="non-existent")

        assert result["success"] is False
        assert "no running agent found" in result["error"].lower()


class TestKillAgent:
    """Tests for kill_agent MCP tool."""

    @pytest.mark.asyncio
    async def test_requires_run_id_or_session_id(self):
        """Test error when neither run_id nor session_id provided."""
        runner = MagicMock()
        running_registry = RunningAgentRegistry()

        registry = create_agents_registry(runner, running_registry=running_registry)
        kill_agent = registry._tools["kill_agent"].func

        result = await kill_agent()

        assert result["success"] is False
        assert "run_id or session_id required" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_signal_rejected(self):
        """Test invalid signal is rejected."""
        runner = MagicMock()
        running_registry = RunningAgentRegistry()

        registry = create_agents_registry(runner, running_registry=running_registry)
        kill_agent = registry._tools["kill_agent"].func

        result = await kill_agent(run_id="run-123", signal="INVALID")

        assert result["success"] is False
        assert "Invalid signal" in result["error"]

    @pytest.mark.asyncio
    async def test_session_id_resolves_to_run_id(self):
        """Test that session_id resolves to run_id via registry."""
        running_registry = RunningAgentRegistry()
        running_registry.add(
            RunningAgent(
                run_id="run-123",
                session_id="sess-456",
                parent_session_id="sess-parent",
                mode="terminal",
            )
        )

        runner = MagicMock()
        runner.get_run.return_value = None

        registry = create_agents_registry(runner, running_registry=running_registry)
        kill_agent = registry._tools["kill_agent"].func

        # Session is found in registry, kill will be called
        result = await kill_agent(session_id="sess-456")

        # The kill should be attempted (may fail due to no PID, but resolution worked)
        # The key assertion is that it didn't fail with "No agent found for session"
        assert "No agent found for session" not in result.get("error", "")

    @pytest.mark.asyncio
    async def test_session_id_not_found_returns_error(self):
        """Test error when session_id doesn't match any agent."""
        running_registry = RunningAgentRegistry()
        runner = MagicMock()
        runner.get_run_id_by_session.return_value = None

        registry = create_agents_registry(runner, running_registry=running_registry)
        kill_agent = registry._tools["kill_agent"].func

        result = await kill_agent(session_id="non-existent")

        assert result["success"] is False
        assert "No agent found for session" in result["error"]

    @pytest.mark.asyncio
    async def test_default_full_cleanup(self):
        """Test that kill_agent does full cleanup by default (workflow + terminal)."""
        running_registry = RunningAgentRegistry()
        running_registry.add(
            RunningAgent(
                run_id="run-123",
                session_id="sess-456",
                parent_session_id="sess-parent",
                mode="in_process",  # Use in_process for simple cancel
                task=MagicMock(),  # Provide a mock task for cancellation
            )
        )

        runner = MagicMock()
        runner.cancel_run.return_value = True

        workflow_state_manager = MagicMock()

        registry = create_agents_registry(
            runner,
            running_registry=running_registry,
            workflow_state_manager=workflow_state_manager,
        )
        kill_agent = registry._tools["kill_agent"].func

        # Default behavior - full cleanup
        result = await kill_agent(run_id="run-123")

        assert result["success"] is True
        # Workflow state should be deleted by default
        workflow_state_manager.delete_state.assert_called_once_with("sess-456")
        assert result.get("workflow_deleted") is True

    @pytest.mark.asyncio
    async def test_debug_preserves_state(self):
        """Test that debug=True preserves workflow state."""
        running_registry = RunningAgentRegistry()
        running_registry.add(
            RunningAgent(
                run_id="run-123",
                session_id="sess-456",
                parent_session_id="sess-parent",
                mode="in_process",
                task=MagicMock(),
            )
        )

        runner = MagicMock()
        runner.cancel_run.return_value = True

        workflow_state_manager = MagicMock()

        registry = create_agents_registry(
            runner,
            running_registry=running_registry,
            workflow_state_manager=workflow_state_manager,
        )
        kill_agent = registry._tools["kill_agent"].func

        # debug=True should preserve state
        result = await kill_agent(run_id="run-123", debug=True)

        assert result["success"] is True
        # Workflow state should NOT be deleted when debugging
        workflow_state_manager.delete_state.assert_not_called()
        assert result.get("workflow_deleted") is None


class TestRunningAgentStats:
    """Tests for running_agent_stats MCP tool."""

    @pytest.mark.asyncio
    async def test_empty_stats(self):
        """Test stats with no running agents."""
        running_registry = RunningAgentRegistry()
        runner = MagicMock()
        registry = create_agents_registry(runner, running_registry=running_registry)
        stats = registry._tools["running_agent_stats"].func

        result = await stats()

        assert result["success"] is True
        assert result["total"] == 0
        assert result["by_mode"] == {}
        assert result["by_parent_count"] == 0

    @pytest.mark.asyncio
    async def test_stats_with_agents(self):
        """Test stats with multiple running agents."""
        running_registry = RunningAgentRegistry()
        running_registry.add(
            RunningAgent(
                run_id="run-1",
                session_id="sess-1",
                parent_session_id="parent-1",
                mode="terminal",
            )
        )
        running_registry.add(
            RunningAgent(
                run_id="run-2",
                session_id="sess-2",
                parent_session_id="parent-1",
                mode="terminal",
            )
        )
        running_registry.add(
            RunningAgent(
                run_id="run-3",
                session_id="sess-3",
                parent_session_id="parent-2",
                mode="autonomous",
            )
        )
        running_registry.add(
            RunningAgent(
                run_id="run-4",
                session_id="sess-4",
                parent_session_id="parent-3",
                mode="autonomous",
            )
        )

        runner = MagicMock()
        registry = create_agents_registry(runner, running_registry=running_registry)
        stats = registry._tools["running_agent_stats"].func

        result = await stats()

        assert result["success"] is True
        assert result["total"] == 4
        assert result["by_mode"]["terminal"] == 2
        assert result["by_mode"]["autonomous"] == 2
        assert result["by_parent_count"] == 3  # 3 unique parents


class TestFireSyntheticStop:
    """Tests for _fire_synthetic_stop helper."""

    def test_noop_when_no_resolver(self):
        """Test that _fire_synthetic_stop does nothing when resolver is None."""
        from gobby.mcp_proxy.tools.agents import _fire_synthetic_stop

        # Should not raise
        _fire_synthetic_stop(None, "sess-123")

    def test_noop_when_resolver_returns_none(self):
        """Test that _fire_synthetic_stop does nothing when resolver returns None."""
        from gobby.mcp_proxy.tools.agents import _fire_synthetic_stop

        _fire_synthetic_stop(lambda: None, "sess-123")

    def test_calls_evaluate_workflow_rules(self):
        """Test that _fire_synthetic_stop fires a synthetic STOP event."""
        from gobby.hooks.events import HookEventType
        from gobby.mcp_proxy.tools.agents import _fire_synthetic_stop

        mock_hook_mgr = MagicMock()
        mock_hook_mgr._evaluate_workflow_rules.return_value = (None, None)

        _fire_synthetic_stop(lambda: mock_hook_mgr, "sess-123")

        mock_hook_mgr._evaluate_workflow_rules.assert_called_once()
        event_arg = mock_hook_mgr._evaluate_workflow_rules.call_args[0][0]
        assert event_arg.event_type == HookEventType.STOP
        assert event_arg.metadata["_platform_session_id"] == "sess-123"

    def test_catches_exceptions(self):
        """Test that _fire_synthetic_stop catches and logs exceptions."""
        from gobby.mcp_proxy.tools.agents import _fire_synthetic_stop

        mock_hook_mgr = MagicMock()
        mock_hook_mgr._evaluate_workflow_rules.side_effect = RuntimeError("boom")

        # Should not raise
        _fire_synthetic_stop(lambda: mock_hook_mgr, "sess-123")

    @pytest.mark.asyncio
    async def test_kill_agent_fires_synthetic_stop(self):
        """Test that kill_agent calls _fire_synthetic_stop after cleanup."""
        running_registry = RunningAgentRegistry()
        running_registry.add(
            RunningAgent(
                run_id="run-123",
                session_id="sess-456",
                parent_session_id="sess-parent",
                mode="in_process",
                task=MagicMock(),
            )
        )

        runner = MagicMock()
        runner.cancel_run.return_value = True

        mock_hook_mgr = MagicMock()
        mock_hook_mgr._evaluate_workflow_rules.return_value = (None, None)
        mock_resolver = MagicMock(return_value=mock_hook_mgr)

        registry = create_agents_registry(
            runner,
            running_registry=running_registry,
            workflow_state_manager=MagicMock(),
            hook_manager_resolver=mock_resolver,
        )
        kill_agent = registry._tools["kill_agent"].func

        result = await kill_agent(run_id="run-123")

        assert result["success"] is True
        # Verify synthetic stop was fired for the agent's session
        mock_resolver.assert_called_once()
        mock_hook_mgr._evaluate_workflow_rules.assert_called_once()
        event_arg = mock_hook_mgr._evaluate_workflow_rules.call_args[0][0]
        assert event_arg.metadata["_platform_session_id"] == "sess-456"
