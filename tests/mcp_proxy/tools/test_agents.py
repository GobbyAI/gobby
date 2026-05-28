"""
Tests for agents.py MCP tools module.

This file tests the agent-related MCP tools:
- spawn_agent: Spawn a subagent with isolation support
- get_agent_result: Get agent run result
- wait_for_agent: Wait for agent run completion
- list_agent_runs: List agent runs for a session
- stop_agent: Stop a running agent (DB only)
- end_agent_run: Complete the caller's own agent run
- kill_agent: Kill a running agent process
- can_spawn_agent: Check if spawning is allowed
- list_running_agents: List active agents from DB
- get_running_agent: Get running agent state from DB
- unregister_agent: Mark agent as failed in DB
- running_agent_stats: Get agent statistics from DB
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.runtime_cleanup import AgentRuntimeCleanupResult
from gobby.events import CompletionEventRegistry
from gobby.events.wake import WakeDispatcher
from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.storage.inter_session_messages import InterSessionMessageManager

pytestmark = pytest.mark.unit


def _make_mock_agent_run(
    run_id: str = "run-123",
    session_id: str | None = "sess-456",
    parent_session_id: str = "sess-parent",
    status: str = "running",
    pid: int | None = None,
    provider: str = "claude",
    **kwargs,
) -> MagicMock:
    """Create a mock AgentRun with to_dict() and to_brief() methods."""
    run = MagicMock()
    run.id = run_id
    run.child_session_id = session_id
    run.parent_session_id = parent_session_id
    run.status = status
    run.pid = pid
    run.provider = provider
    run.task_id = kwargs.get("task_id")
    run.started_at = kwargs.get("started_at")
    run.tmux_session_name = kwargs.get("tmux_session_name")
    run.worktree_id = kwargs.get("worktree_id")
    run.clone_id = kwargs.get("clone_id")
    run.workflow_name = kwargs.get("workflow_name")
    run.agent_name = kwargs.get("agent_name")
    run.model = kwargs.get("model")

    run.to_dict.return_value = {
        "run_id": run_id,
        "id": run_id,
        "session_id": session_id,
        "parent_session_id": parent_session_id,
        "status": status,
        "pid": pid,
        "agent_name": kwargs.get("agent_name"),
        "workflow_name": kwargs.get("workflow_name"),
        "provider": provider,
        "model": kwargs.get("model"),
        "terminal_type": kwargs.get("terminal_type"),
    }
    run.to_brief.return_value = {
        "run_id": run_id,
        "session_id": session_id,
        "parent_session_id": parent_session_id,
        "pid": pid,
        "agent_name": kwargs.get("agent_name"),
        "workflow_name": kwargs.get("workflow_name"),
        "provider": provider,
        "model": kwargs.get("model"),
        "status": status,
    }
    return run


def _make_runner_with_run_storage() -> MagicMock:
    """Create a mock runner with a mock run_storage (LocalAgentRunManager)."""
    runner = MagicMock()
    runner.run_storage = MagicMock()
    runner.run_storage.list_active.return_value = []
    runner.run_storage.list_by_parent.return_value = []
    runner.run_storage.get.return_value = None
    runner.run_storage.get_by_session.return_value = None
    return runner


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
            "wait_for_agent",
            "list_agent_runs",
            "stop_agent",
            "cancel_stale_helpers",
            "end_agent_run",
            "kill_agent",
            "can_spawn_agent",
            "list_running_agents",
            "get_running_agent",
            "unregister_agent",
            "running_agent_stats",
        ]

        for tool_name in expected_tools:
            assert registry.get_schema(tool_name) is not None, f"Missing tool: {tool_name}"

        assert registry.get_schema("list_agents") is None

    def test_kill_agent_schema_accepts_stop(self) -> None:
        """Regression: CLI kill -s sends stop through MCP validation."""
        runner = MagicMock()
        registry = create_agents_registry(runner)

        schema = registry.get_schema("kill_agent")

        assert schema is not None
        assert schema["inputSchema"]["properties"]["stop"]["type"] == "boolean"

    def test_accepts_running_registry_for_backward_compat(self) -> None:
        """Test that running_registry param is accepted but ignored."""
        runner = MagicMock()

        # Should not raise — param is accepted for backward compat
        registry = create_agents_registry(runner, running_registry=MagicMock())
        assert registry is not None

    def test_agents_py_under_monolith_limit(self) -> None:
        """Guard against growing the non-test agents module into a monolith."""
        repo_root = Path(__file__).parents[3]
        agents_py = repo_root / "src/gobby/mcp_proxy/tools/agents.py"

        assert agents_py.read_text(encoding="utf-8").count("\n") < 1000


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


class TestWaitForAgent:
    """Tests for wait_for_agent MCP tool."""

    @pytest.mark.asyncio
    async def test_completed_run_returns_immediately(self):
        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.status = "success"
        mock_run.result = "done"
        mock_run.error = None
        mock_run.provider = "claude"
        mock_run.model = "opus"
        mock_run.prompt = "merge"
        mock_run.tool_calls_count = 4
        mock_run.turns_used = 2
        mock_run.started_at = "2026-05-20T00:00:00Z"
        mock_run.completed_at = "2026-05-20T00:01:00Z"
        mock_run.child_session_id = "child-session"
        mock_run.terminal_reason = None

        runner = MagicMock()
        runner.get_run.return_value = mock_run

        registry = create_agents_registry(runner)
        wait_for_agent = registry._tools["wait_for_agent"].func

        result = await wait_for_agent(run_id="run-123", timeout_seconds=0)

        assert result["success"] is True
        assert result["completed"] is True
        assert result["status"] == "success"
        assert result["result"] == "done"
        assert "prompt" not in result
        assert result["tool_calls_count"] == 4
        assert result["turns_used"] == 2

    @pytest.mark.asyncio
    async def test_running_run_times_out_with_latest_status(self):
        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.status = "running"
        mock_run.result = None
        mock_run.error = None
        mock_run.provider = "claude"
        mock_run.model = "opus"
        mock_run.prompt = "merge"
        mock_run.tool_calls_count = 1
        mock_run.turns_used = 1
        mock_run.started_at = "2026-05-20T00:00:00Z"
        mock_run.completed_at = None
        mock_run.child_session_id = "child-session"
        mock_run.terminal_reason = None

        runner = MagicMock()
        runner.get_run.return_value = mock_run

        registry = create_agents_registry(runner)
        wait_for_agent = registry._tools["wait_for_agent"].func

        result = await wait_for_agent(run_id="run-123", timeout_seconds=0)

        assert result["success"] is True
        assert result["completed"] is False
        assert result["status"] == "running"
        assert result["timeout_seconds"] == 0.0
        assert result["requested_timeout_seconds"] == 0.0
        assert "prompt" not in result
        assert result["tool_calls_count"] == 1
        assert result["turns_used"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("requested_timeout", [120, 300])
    async def test_long_running_wait_returns_before_transport_boundary(self, requested_timeout):
        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.status = "running"
        mock_run.result = None
        mock_run.error = None
        mock_run.provider = "claude"
        mock_run.model = "opus"
        mock_run.prompt = "merge"
        mock_run.tool_calls_count = 1
        mock_run.turns_used = 1
        mock_run.started_at = "2026-05-20T00:00:00Z"
        mock_run.completed_at = None
        mock_run.child_session_id = "child-session"
        mock_run.terminal_reason = None

        runner = MagicMock()
        runner.get_run.return_value = mock_run

        registry = create_agents_registry(runner)
        wait_for_agent = registry._tools["wait_for_agent"].func

        with patch("gobby.mcp_proxy.tools.agents.time.monotonic", side_effect=[0.0, 116.0]):
            result = await wait_for_agent(
                run_id="run-123",
                timeout_seconds=requested_timeout,
                poll_interval_seconds=5,
            )

        assert result["success"] is True
        assert result["completed"] is False
        assert result["status"] == "running"
        assert result["timeout_seconds"] == 115.0
        assert result["requested_timeout_seconds"] == float(requested_timeout)

    @pytest.mark.asyncio
    async def test_shorter_wait_timeout_is_not_reduced(self):
        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.status = "running"
        mock_run.result = None
        mock_run.error = None
        mock_run.provider = "claude"
        mock_run.model = "opus"
        mock_run.prompt = "merge"
        mock_run.tool_calls_count = 1
        mock_run.turns_used = 1
        mock_run.started_at = "2026-05-20T00:00:00Z"
        mock_run.completed_at = None
        mock_run.child_session_id = "child-session"
        mock_run.terminal_reason = None

        runner = MagicMock()
        runner.get_run.return_value = mock_run

        registry = create_agents_registry(runner)
        wait_for_agent = registry._tools["wait_for_agent"].func

        with patch("gobby.mcp_proxy.tools.agents.time.monotonic", side_effect=[0.0, 5.1]):
            result = await wait_for_agent(
                run_id="run-123",
                timeout_seconds=5,
                poll_interval_seconds=1,
            )

        assert result["success"] is True
        assert result["completed"] is False
        assert result["status"] == "running"
        assert result["timeout_seconds"] == 5.0
        assert result["requested_timeout_seconds"] == 5.0

    @pytest.mark.asyncio
    async def test_wait_polls_until_run_completes(self):
        running_run = MagicMock()
        running_run.id = "run-123"
        running_run.status = "running"
        running_run.result = None
        running_run.error = None
        running_run.provider = "claude"
        running_run.model = "opus"
        running_run.prompt = "merge"
        running_run.tool_calls_count = 1
        running_run.turns_used = 1
        running_run.started_at = "2026-05-20T00:00:00Z"
        running_run.completed_at = None
        running_run.child_session_id = "child-session"
        running_run.terminal_reason = None

        completed_run = MagicMock()
        completed_run.id = "run-123"
        completed_run.status = "success"
        completed_run.result = "done"
        completed_run.error = None
        completed_run.provider = "claude"
        completed_run.model = "opus"
        completed_run.prompt = "merge"
        completed_run.tool_calls_count = 3
        completed_run.turns_used = 2
        completed_run.started_at = "2026-05-20T00:00:00Z"
        completed_run.completed_at = "2026-05-20T00:01:00Z"
        completed_run.child_session_id = "child-session"
        completed_run.terminal_reason = None

        runner = MagicMock()
        runner.get_run.side_effect = [running_run, completed_run]

        registry = create_agents_registry(runner)
        wait_for_agent = registry._tools["wait_for_agent"].func

        with patch("gobby.mcp_proxy.tools.agents.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await wait_for_agent(
                run_id="run-123",
                timeout_seconds=5,
                poll_interval_seconds=0.1,
            )

        assert result["success"] is True
        assert result["completed"] is True
        assert result["status"] == "success"
        sleep.assert_awaited_once()


class TestListAgentRuns:
    """Tests for list_agent_runs MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_runs(self):
        """Test empty list when no runs exist."""
        runner = MagicMock()
        runner.list_runs.return_value = []

        registry = create_agents_registry(runner)
        list_agent_runs = registry._tools["list_agent_runs"].func

        result = await list_agent_runs(parent_session_id="sess-123")

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
        list_agent_runs = registry._tools["list_agent_runs"].func

        result = await list_agent_runs(parent_session_id="sess-123")

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
        list_agent_runs = registry._tools["list_agent_runs"].func

        await list_agent_runs(parent_session_id="sess-123", status="running")

        runner.list_runs.assert_called_once_with("sess-123", status="running", limit=20)
        assert runner.list_runs.call_count == 1
        assert runner.list_runs.call_args is not None

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        """Test limit parameter is passed to runner."""
        runner = MagicMock()
        runner.list_runs.return_value = []

        registry = create_agents_registry(runner)
        list_agent_runs = registry._tools["list_agent_runs"].func

        await list_agent_runs(parent_session_id="sess-123", limit=50)

        runner.list_runs.assert_called_once_with("sess-123", status=None, limit=50)
        assert runner.list_runs.call_count == 1
        assert runner.list_runs.call_args is not None


class TestStopAgent:
    """Tests for stop_agent MCP tool."""

    @pytest.mark.asyncio
    async def test_successful_stop(self):
        """Test successful agent stop."""
        runner = _make_runner_with_run_storage()
        runner.get_run.return_value = _make_mock_agent_run(status="running")
        runner.cancel_run.return_value = True
        runtime_db = object()

        registry = create_agents_registry(runner, db=runtime_db)
        stop_agent = registry._tools["stop_agent"].func

        with (
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
            patch(
                "gobby.mcp_proxy.tools.agents.cleanup_agent_runtime_state",
                return_value=AgentRuntimeCleanupResult(
                    dispatch_mutex_rows=1,
                    workflow_instance_rows=1,
                ),
            ) as cleanup,
        ):
            result = await stop_agent(run_id="run-123")

        assert result["success"] is True
        assert "stopped" in result["message"]
        assert result["terminal_reason"] == "user_cancelled"
        runner.cancel_run.assert_called_once_with("run-123")
        cleanup.assert_called_once_with(
            runtime_db,
            run_id="run-123",
            child_session_id="sess-456",
        )

    @pytest.mark.asyncio
    async def test_successful_stop_passes_task_manager_to_cancellation_helper(self):
        """Test stop_agent wires task recovery dependencies into fallback cancellation."""
        runner = _make_runner_with_run_storage()
        runner.get_run.return_value = _make_mock_agent_run(status="running")
        task_manager = MagicMock()

        registry = create_agents_registry(runner, task_manager=task_manager)
        stop_agent = registry._tools["stop_agent"].func

        with (
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ) as kill_process,
            patch(
                "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
                new_callable=AsyncMock,
                return_value=True,
            ) as terminalize,
            patch("gobby.mcp_proxy.tools.agents.cleanup_agent_runtime_state"),
        ):
            result = await stop_agent(run_id="run-123")

        assert result["success"] is True
        assert result["run_id"] == "run-123"
        assert result["status"] == "cancelled"
        assert result["terminal_reason"] == "user_cancelled"
        runner.get_run.assert_called_once_with("run-123")
        kill_process.assert_awaited_once()
        assert kill_process.call_args.args[0] is runner.get_run.return_value
        assert kill_process.call_args.kwargs["signal_name"] == "TERM"
        assert kill_process.call_args.kwargs["close_terminal"] is True
        terminalize.assert_awaited_once_with(
            runner=runner,
            run_id="run-123",
            terminal_reason="user_cancelled",
            lifecycle_monitor=None,
            completion_registry=None,
            task_manager=task_manager,
            message="Agent run-123 cancelled",
        )

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
    """Tests for list_running_agents MCP tool (DB-backed via LocalAgentRunManager)."""

    def _make_agents(self) -> list[MagicMock]:
        """Create test agents."""
        return [
            _make_mock_agent_run(
                run_id="run-1",
                session_id="sess-1",
                parent_session_id="parent-1",
                pid=1001,
            ),
            _make_mock_agent_run(
                run_id="run-2",
                session_id="sess-2",
                parent_session_id="parent-1",
                pid=1002,
            ),
            _make_mock_agent_run(
                run_id="run-3",
                session_id="sess-3",
                parent_session_id="parent-2",
                pid=1003,
            ),
        ]

    @pytest.mark.asyncio
    async def test_list_all_running_agents(self):
        """Test listing all running agents."""
        runner = _make_runner_with_run_storage()
        agents = self._make_agents()
        runner.run_storage.list_active.return_value = agents

        registry = create_agents_registry(runner)
        list_running = registry._tools["list_running_agents"].func

        result = await list_running()

        assert result["success"] is True
        assert result["count"] == 3
        assert len(result["agents"]) == 3
        assert result["scope"] == "all"
        runner.run_storage.list_active.assert_called_once_with(limit=100)

    @pytest.mark.asyncio
    async def test_filter_by_parent_session(self):
        """Test filtering by parent session ID."""
        runner = _make_runner_with_run_storage()
        agents = self._make_agents()
        parent1_agents = [a for a in agents if a.parent_session_id == "parent-1"]
        runner.run_storage.list_by_parent.return_value = parent1_agents

        registry = create_agents_registry(runner)
        list_running = registry._tools["list_running_agents"].func

        result = await list_running(parent_session_id="parent-1")

        assert result["success"] is True
        assert result["count"] == 2
        runner.run_storage.list_by_parent.assert_called_once_with(
            "parent-1",
            limit=100,
            status=None,
        )

    @pytest.mark.asyncio
    async def test_default_all_scope_ignores_current_session_context(self):
        """Default listing is build-wide even when MCP seeds caller session context."""
        from gobby.utils.session_context import session_context_for_test

        runner = _make_runner_with_run_storage()
        agents = self._make_agents()
        runner.run_storage.list_active.return_value = agents

        registry = create_agents_registry(runner)
        list_running = registry._tools["list_running_agents"].func

        with session_context_for_test("caller-session"):
            result = await list_running()

        assert result["success"] is True
        assert result["count"] == 3
        runner.run_storage.list_active.assert_called_once_with(limit=100)
        runner.run_storage.list_by_parent.assert_not_called()

    @pytest.mark.asyncio
    async def test_parent_scope_uses_current_session_context_when_requested(self):
        """Callers can still ask for direct children of the current session."""
        from gobby.utils.session_context import session_context_for_test

        runner = _make_runner_with_run_storage()
        parent_agents = [a for a in self._make_agents() if a.parent_session_id == "parent-1"]
        runner.run_storage.list_by_parent.return_value = parent_agents

        registry = create_agents_registry(runner)
        list_running = registry._tools["list_running_agents"].func

        with session_context_for_test("parent-1"):
            result = await list_running(scope="parent")

        assert result["success"] is True
        assert result["count"] == 2
        assert result["scope"] == "parent"
        runner.run_storage.list_by_parent.assert_called_once_with(
            "parent-1",
            limit=100,
            status=None,
        )

    @pytest.mark.asyncio
    async def test_running_status_uses_cli_equivalent_query(self):
        """status='running' uses the same storage path as CLI --status running."""
        runner = _make_runner_with_run_storage()
        running_agents = [self._make_agents()[0]]
        runner.run_storage.list_running.return_value = running_agents

        registry = create_agents_registry(runner)
        list_running = registry._tools["list_running_agents"].func

        result = await list_running(status="running", limit=25)

        assert result["success"] is True
        assert result["count"] == 1
        assert result["status"] == "running"
        runner.run_storage.list_running.assert_called_once_with(limit=25)

    @pytest.mark.asyncio
    async def test_list_includes_agent_identity(self):
        """List payloads expose agent identity so orchestrators can filter workers."""
        runner = _make_runner_with_run_storage()
        runner.run_storage.list_by_parent.return_value = [
            _make_mock_agent_run(
                run_id="run-worker",
                session_id="sess-worker",
                parent_session_id="parent-1",
                agent_name="merge-worker",
                workflow_name="merge-worker",
                model="sonnet",
            )
        ]

        registry = create_agents_registry(runner)
        list_running = registry._tools["list_running_agents"].func

        result = await list_running(parent_session_id="parent-1")

        assert result["agents"][0]["agent_name"] == "merge-worker"
        assert result["agents"][0]["workflow_name"] == "merge-worker"
        assert result["agents"][0]["model"] == "sonnet"


class TestGetRunningAgent:
    """Tests for get_running_agent MCP tool (DB-backed)."""

    @pytest.mark.asyncio
    async def test_agent_found(self):
        """Test getting an existing running agent."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="sess-parent",
            pid=12345,
            provider="claude",
            status="running",
        )
        runner.run_storage.get.return_value = mock_run

        registry = create_agents_registry(runner)
        get_running = registry._tools["get_running_agent"].func

        result = await get_running(run_id="run-123")

        assert result["success"] is True
        assert result["agent"]["run_id"] == "run-123"
        assert result["agent"]["pid"] == 12345

    @pytest.mark.asyncio
    async def test_agent_not_found(self):
        """Test error when agent not found."""
        runner = _make_runner_with_run_storage()
        runner.run_storage.get.return_value = None

        registry = create_agents_registry(runner)
        get_running = registry._tools["get_running_agent"].func

        result = await get_running(run_id="non-existent")

        assert result["success"] is False
        assert "no running agent found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_completed_agent_not_returned(self):
        """Test that completed agents are not returned as 'running'."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(run_id="run-123", status="success")
        runner.run_storage.get.return_value = mock_run

        registry = create_agents_registry(runner)
        get_running = registry._tools["get_running_agent"].func

        result = await get_running(run_id="run-123")

        assert result["success"] is False
        assert "no running agent found" in result["error"].lower()


class TestUnregisterAgent:
    """Tests for unregister_agent MCP tool (DB-backed via agent_run_manager.fail)."""

    @pytest.mark.asyncio
    async def test_successful_unregistration(self):
        """Test successful agent unregistration (marks as failed in DB)."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(run_id="run-123", status="running")
        runner.run_storage.get.return_value = mock_run

        registry = create_agents_registry(runner)
        unregister = registry._tools["unregister_agent"].func

        result = await unregister(run_id="run-123")

        assert result["success"] is True
        assert "Unregistered" in result["message"]
        runner.run_storage.fail.assert_called_once_with("run-123", error="Unregistered")

    @pytest.mark.asyncio
    async def test_unregister_not_found(self):
        """Test error when agent not found."""
        runner = _make_runner_with_run_storage()
        runner.run_storage.get.return_value = None

        registry = create_agents_registry(runner)
        unregister = registry._tools["unregister_agent"].func

        result = await unregister(run_id="non-existent")

        assert result["success"] is False
        assert "no agent found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_unregister_already_completed(self):
        """Test unregistering an already-completed agent returns success with message."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(run_id="run-123", status="success")
        runner.run_storage.get.return_value = mock_run

        registry = create_agents_registry(runner)
        unregister = registry._tools["unregister_agent"].func

        result = await unregister(run_id="run-123")

        assert result["success"] is True
        assert "already in status" in result["message"]
        runner.run_storage.fail.assert_not_called()


class TestKillAgent:
    """Tests for kill_agent MCP tool."""

    @pytest.mark.asyncio
    async def test_requires_run_id_or_session_id(self):
        """Test error when neither run_id nor session_id provided."""
        runner = _make_runner_with_run_storage()

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        result = await kill_agent()

        assert result["success"] is False
        assert "run_id or session_id required" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_signal_rejected(self):
        """Test invalid signal is rejected."""
        runner = _make_runner_with_run_storage()

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        result = await kill_agent(run_id="run-123", signal="INVALID")

        assert result["success"] is False
        assert "Invalid signal" in result["error"]

    @pytest.mark.asyncio
    async def test_session_id_resolves_to_run_id(self):
        """Test that session_id resolves to run_id via DB."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="sess-parent",
        )
        runner.run_storage.get_by_session.return_value = mock_run
        runner.get_run.return_value = mock_run

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        with patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ):
            result = await kill_agent(session_id="sess-456")

        # The key assertion: it found the agent via session_id
        assert "No agent found for session" not in result.get("error", "")

    @pytest.mark.asyncio
    async def test_session_id_not_found_returns_error(self):
        """Test error when session_id doesn't match any agent."""
        runner = _make_runner_with_run_storage()
        runner.run_storage.get_by_session.return_value = None
        runner.get_run_id_by_session.return_value = None

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        result = await kill_agent(session_id="non-existent")

        assert result["success"] is False
        assert "No agent found for session" in result["error"]

    @pytest.mark.asyncio
    async def test_default_full_cleanup(self):
        """Test that kill_agent does full cleanup by default."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="sess-parent",
        )
        runner.get_run.return_value = mock_run
        runner.cancel_run.return_value = True

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        with patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ):
            result = await kill_agent(run_id="run-123")

        assert result["success"] is True
        assert result["workflow_stopped"] is True
        runner.cancel_run.assert_called_once_with("run-123")

    @pytest.mark.asyncio
    async def test_stop_false_kills_without_terminalizing_workflow(self):
        """CLI kill without --stop should not cancel workflow state."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="sess-parent",
        )
        runner.get_run.return_value = mock_run

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        with (
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ) as kill_process,
            patch(
                "gobby.mcp_proxy.tools.agents._cleanup_terminal_artifacts",
                new_callable=AsyncMock,
            ) as cleanup,
        ):
            result = await kill_agent(run_id="run-123", stop=False)

        assert result["success"] is True
        assert result["workflow_stopped"] is False
        assert kill_process.call_args.kwargs["close_terminal"] is True
        cleanup.assert_not_awaited()
        runner.cancel_run.assert_not_called()
        runner.complete_run.assert_not_called()
        runner.run_storage.fail.assert_not_called()

    @pytest.mark.asyncio
    async def test_debug_preserves_state(self):
        """Test that debug=True preserves workflow state."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="sess-parent",
        )
        runner.get_run.return_value = mock_run
        runner.cancel_run.return_value = True

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        with patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ):
            result = await kill_agent(run_id="run-123", debug=True)

        assert result["success"] is True


class TestEndAgentRun:
    """Tests for end_agent_run MCP tool."""

    @pytest.mark.asyncio
    async def test_requires_active_session_context(self) -> None:
        runner = _make_runner_with_run_storage()
        registry = create_agents_registry(runner)

        result = await registry._tools["end_agent_run"].func()

        assert result["success"] is False
        assert "No active session context" in result["error"]

    @pytest.mark.asyncio
    async def test_resolves_current_session_and_notifies_success(self) -> None:
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="sess-parent",
        )
        runner.run_storage.get_by_session.return_value = mock_run
        runner.get_run.return_value = mock_run
        runner.complete_run.return_value = True
        completion_registry = MagicMock()
        completion_registry.get_result.return_value = None
        completion_registry.notify = AsyncMock()

        registry = create_agents_registry(runner, completion_registry=completion_registry)

        from gobby.utils.session_context import session_context_for_test

        with (
            session_context_for_test("sess-456"),
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
        ):
            result = await registry._tools["end_agent_run"].func()

        assert result == {"success": True, "run_id": "run-123", "status": "success"}
        runner.complete_run.assert_called_once_with("run-123", result=None)
        completion_registry.notify.assert_awaited_once_with(
            "run-123",
            {"status": "success", "run_id": "run-123"},
            message="Agent run-123 completed",
        )

    @pytest.mark.asyncio
    async def test_returns_error_when_session_has_no_agent_run(self) -> None:
        runner = _make_runner_with_run_storage()
        runner.get_run_id_by_session.return_value = None
        registry = create_agents_registry(runner)

        from gobby.utils.session_context import session_context_for_test

        with session_context_for_test("sess-456"):
            result = await registry._tools["end_agent_run"].func()

        assert result["success"] is False
        assert "No agent found for session sess-456" == result["error"]

    @pytest.mark.asyncio
    async def test_unsubscribed_memory_helper_end_agent_run_does_not_notify_parent(
        self, temp_db
    ) -> None:
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="sess-parent",
        )
        runner.run_storage.get_by_session.return_value = mock_run
        runner.get_run.return_value = mock_run
        runner.complete_run.return_value = True

        ism_manager = InterSessionMessageManager(temp_db)
        session_manager = MagicMock()
        session_manager.get.return_value = MagicMock(id="sess-parent", agent_depth=0)
        wake_dispatcher = WakeDispatcher(session_manager, ism_manager)
        completion_registry = CompletionEventRegistry(wake_callback=wake_dispatcher.wake)
        registry = create_agents_registry(
            runner,
            completion_registry=completion_registry,
            session_manager=session_manager,
        )

        from gobby.utils.session_context import session_context_for_test

        with (
            session_context_for_test("sess-456"),
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
        ):
            result = await registry._tools["end_agent_run"].func()

        assert result == {"success": True, "run_id": "run-123", "status": "success"}
        assert ism_manager.list_messages("sess-parent") == []
        assert not ism_manager.has_completion_notification(
            "sess-parent",
            "completion_notification",
            "run-123",
        )


class TestKillAgentSelfTerminationViaRunId:
    """Tests for self-termination detection via run_id path using _context."""

    @pytest.mark.asyncio
    async def test_run_id_self_termination_defaults_to_success(self):
        """When agent calls kill_agent(run_id=...) and _context matches, default to success."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="sess-parent",
        )
        runner.get_run.return_value = mock_run
        runner.complete_run.return_value = True

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        # Set session context matching the agent's session (self-termination)
        from gobby.utils.session_context import session_context_for_test

        with (
            session_context_for_test("sess-456"),
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
        ):
            result = await kill_agent(run_id="run-123")

        assert result["success"] is True
        # Should call complete_run (success), not cancel_run (cancelled)
        runner.complete_run.assert_called_once_with("run-123", result=None)
        runner.cancel_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_id_parent_kill_defaults_to_cancelled(self):
        """When parent kills agent via run_id, session context doesn't match, default to cancelled."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="sess-parent",
        )
        runner.get_run.return_value = mock_run
        runner.cancel_run.return_value = True

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        # Set session context with different session_id (parent killing child)
        from gobby.utils.session_context import session_context_for_test

        with (
            session_context_for_test("sess-parent"),
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
        ):
            result = await kill_agent(run_id="run-123")

        assert result["success"] is True
        runner.cancel_run.assert_called_once_with("run-123")
        runner.complete_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_id_no_context_defaults_to_cancelled(self):
        """Without _context, run_id path defaults to cancelled (backward compat)."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="sess-parent",
        )
        runner.get_run.return_value = mock_run
        runner.cancel_run.return_value = True

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        with patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ):
            result = await kill_agent(run_id="run-123")

        assert result["success"] is True
        runner.cancel_run.assert_called_once_with("run-123")
        runner.complete_run.assert_not_called()


class TestRunningAgentStats:
    """Tests for running_agent_stats MCP tool (DB-backed)."""

    @pytest.mark.asyncio
    async def test_empty_stats(self):
        """Test stats with no running agents."""
        runner = _make_runner_with_run_storage()
        runner.run_storage.list_active.return_value = []

        registry = create_agents_registry(runner)
        stats = registry._tools["running_agent_stats"].func

        result = await stats()

        assert result["success"] is True
        assert result["total"] == 0
        assert result["by_parent_count"] == 0

    @pytest.mark.asyncio
    async def test_stats_with_agents(self):
        """Test stats with multiple running agents."""
        runner = _make_runner_with_run_storage()
        runner.run_storage.list_active.return_value = [
            _make_mock_agent_run(
                run_id="run-1",
                parent_session_id="parent-1",
            ),
            _make_mock_agent_run(
                run_id="run-2",
                parent_session_id="parent-1",
            ),
            _make_mock_agent_run(
                run_id="run-3",
                parent_session_id="parent-2",
            ),
            _make_mock_agent_run(
                run_id="run-4",
                parent_session_id="parent-3",
            ),
        ]

        registry = create_agents_registry(runner)
        stats = registry._tools["running_agent_stats"].func

        result = await stats()

        assert result["success"] is True
        assert result["total"] == 4
        assert result["by_parent_count"] == 3  # 3 unique parents


class TestFireSyntheticStop:
    """Tests for _fire_synthetic_stop helper."""

    def test_noop_when_no_resolver(self):
        """Test that _fire_synthetic_stop does nothing when resolver is None."""
        from gobby.mcp_proxy.tools.agents import _fire_synthetic_stop

        result = _fire_synthetic_stop(None, "sess-123")
        assert result is None

    def test_noop_when_resolver_returns_none(self):
        """Test that _fire_synthetic_stop does nothing when resolver returns None."""
        from gobby.mcp_proxy.tools.agents import _fire_synthetic_stop

        result = _fire_synthetic_stop(lambda: None, "sess-123")
        assert result is None

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

        result = _fire_synthetic_stop(lambda: mock_hook_mgr, "sess-123")
        assert result is None
        assert mock_hook_mgr._evaluate_workflow_rules.call_count == 1

    @pytest.mark.asyncio
    async def test_kill_agent_fires_synthetic_stop(self):
        """Test that kill_agent calls _fire_synthetic_stop after cleanup."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="sess-parent",
        )
        runner.get_run.return_value = mock_run
        runner.cancel_run.return_value = True

        mock_hook_mgr = MagicMock()
        mock_hook_mgr._evaluate_workflow_rules.return_value = (None, None)
        mock_resolver = MagicMock(return_value=mock_hook_mgr)

        registry = create_agents_registry(
            runner,
            hook_manager_resolver=mock_resolver,
        )
        kill_agent = registry._tools["kill_agent"].func

        with patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ):
            result = await kill_agent(run_id="run-123")

        assert result["success"] is True
        # Verify synthetic stop was fired for the agent's session
        mock_resolver.assert_called_once()
        mock_hook_mgr._evaluate_workflow_rules.assert_called_once()
        event_arg = mock_hook_mgr._evaluate_workflow_rules.call_args[0][0]
        assert event_arg.metadata["_platform_session_id"] == "sess-456"


class TestCompleteSelfTerminatedRunSignoffMessage:
    """`_complete_self_terminated_run` reads the agent's adversary_verdict
    session var and threads it via signoff_message in the notify_result dict
    so the wake dispatcher's content-fallback chain surfaces the verdict to
    the parent's P2P inbox.
    """

    @pytest.mark.asyncio
    async def test_signoff_message_set_when_adversary_verdict_present(self):
        from gobby.mcp_proxy.tools.agents import _complete_self_terminated_run

        run = MagicMock()
        run.id = "run-xyz"
        run.child_session_id = "child-sess-1"
        run.tmux_session_name = "tmux-1"
        runner = MagicMock()
        kill_db = MagicMock()
        completion_registry = MagicMock()
        completion_registry.get_result = MagicMock(return_value=None)
        completion_registry.notify = AsyncMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
            patch(
                "gobby.mcp_proxy.tools.agents.complete_and_notify_agent_run",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_complete,
            patch(
                "gobby.mcp_proxy.tools.agents._cleanup_terminal_artifacts",
                new_callable=AsyncMock,
            ),
            patch("gobby.workflows.state_manager.SessionVariableManager") as svm_cls,
        ):
            svm_cls.return_value.get_variables.return_value = {
                "adversary_verdict": "REJECTED: round 5, 1 blocking (sample)"
            }
            await _complete_self_terminated_run(
                runner=runner,
                run=run,
                kill_db=kill_db,
                completion_registry=completion_registry,
                session_manager=None,
                hook_manager_resolver=None,
            )

        notify_result = mock_complete.call_args.kwargs["notify_result"]
        assert notify_result["signoff_message"] == "REJECTED: round 5, 1 blocking (sample)"
        assert notify_result["status"] == "success"
        assert notify_result["run_id"] == "run-xyz"

    @pytest.mark.asyncio
    async def test_signoff_message_omitted_when_var_unset(self):
        """Non-adversary runs leave adversary_verdict unset; the result dict
        must NOT carry a signoff_message so the existing fallback message wins.
        """
        from gobby.mcp_proxy.tools.agents import _complete_self_terminated_run

        run = MagicMock()
        run.id = "run-abc"
        run.child_session_id = "child-sess-2"
        run.tmux_session_name = "tmux-2"
        runner = MagicMock()
        kill_db = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
            patch(
                "gobby.mcp_proxy.tools.agents.complete_and_notify_agent_run",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_complete,
            patch(
                "gobby.mcp_proxy.tools.agents._cleanup_terminal_artifacts",
                new_callable=AsyncMock,
            ),
            patch("gobby.workflows.state_manager.SessionVariableManager") as svm_cls,
        ):
            svm_cls.return_value.get_variables.return_value = {}  # var unset
            await _complete_self_terminated_run(
                runner=runner,
                run=run,
                kill_db=kill_db,
                completion_registry=None,
                session_manager=None,
                hook_manager_resolver=None,
            )

        notify_result = mock_complete.call_args.kwargs["notify_result"]
        assert "signoff_message" not in notify_result
        assert notify_result["status"] == "success"
        assert notify_result["run_id"] == "run-abc"
