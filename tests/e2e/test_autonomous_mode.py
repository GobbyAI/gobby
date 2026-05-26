"""E2E tests for autonomous task-readiness flows.

These tests verify the surrounding infrastructure for autonomous scheduling:
- usage reporting remains available
- ready tasks can be discovered
- task suggestion returns runnable work

Note: Full agent auto-spawning requires LLM API keys which are disabled in E2E tests.
"""

import uuid

import pytest

from tests.e2e.conftest import (
    CLIEventSimulator,
    DaemonInstance,
    MCPTestClient,
)

pytestmark = pytest.mark.e2e


def unwrap_result(result: dict) -> dict:
    """Unwrap MCP tool call result from wrapper response."""
    if "result" in result:
        return result["result"]
    return result


class TestAutonomousModeToolsAvailability:
    """Tests to verify autonomous mode tools are properly registered."""

    def test_usage_tools_are_registered(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Verify usage reporting tools are available on gobby-metrics."""
        tools = mcp_client.list_tools(server_name="gobby-metrics")
        tool_names = [t["name"] for t in tools]

        assert "get_usage_report" in tool_names, "Missing get_usage_report"
        assert "get_budget_status" not in tool_names

    def test_agent_tools_are_registered(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Verify agent spawning tools are available."""
        # Check gobby-agents tools - spawn_agent is the unified tool that replaces
        # start_agent and spawn_agent_in_clone
        agent_tools = mcp_client.list_tools(server_name="gobby-agents")
        agent_tool_names = [t["name"] for t in agent_tools]
        assert "spawn_agent" in agent_tool_names, "Missing spawn_agent tool"


class TestAutonomousQueries:
    """Tests for autonomous-facing query tools."""

    def test_get_usage_report_returns_structure(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test get_usage_report returns the expected usage structure."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-metrics",
            tool_name="get_usage_report",
            arguments={},
        )
        result = unwrap_result(raw_result)

        assert "error" not in result, f"get_usage_report failed: {result}"
        usage = result.get("usage", {})

        assert "total_input_tokens" in usage, f"Missing total_input_tokens: {result}"
        assert "total_output_tokens" in usage, f"Missing total_output_tokens: {result}"
        assert "session_count" in usage, f"Missing session_count: {result}"

    def test_list_ready_tasks_returns_structure(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test list_ready_tasks returns correct structure."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-tasks",
            tool_name="list_ready_tasks",
            arguments={},
        )
        result = unwrap_result(raw_result)

        # Should return tasks list (may not have explicit success field)
        assert "tasks" in result, f"Missing tasks: {result}"
        assert isinstance(result["tasks"], list), f"tasks should be list: {result}"
        assert "count" in result, f"Missing count: {result}"


class TestAutonomousSpawningGate:
    """Tests for the autonomous scheduling gate (task readiness)."""

    def test_autonomous_gate_with_ready_tasks(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test that ready tasks are discovered for autonomous scheduling.

        Note: Actual auto-spawn is not tested here.
        """
        # Setup - register project and session
        project_result = cli_events.register_test_project(
            project_id="e2e-test-project",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        session_external_id = f"autonomous-gate-{uuid.uuid4().hex[:8]}"
        session_result = cli_events.register_session(
            external_id=session_external_id,
            machine_id="test-machine",
            source="Claude Code",
            cwd=str(daemon_instance.project_dir),
        )
        session_id = session_result["id"]
        mcp_client.session_id = session_id

        # Create epic with subtasks
        raw_result = mcp_client.call_tool(
            server_name="gobby-tasks",
            tool_name="create_task",
            arguments={
                "title": "Autonomous Mode Test Epic",
                "description": "Epic for testing autonomous mode gate",
                "task_type": "epic",
                "category": "code",
                "implementation_domain": "backend",
                "validation_criteria": "Tests pass and task is functional",
            },
        )
        result = unwrap_result(raw_result)
        assert result.get("id") is not None, f"Epic creation failed: {result}"
        epic_id = result["id"]

        # Create 2 independent subtasks
        subtask_ids = []
        for i in range(1, 3):
            raw_result = mcp_client.call_tool(
                server_name="gobby-tasks",
                tool_name="create_task",
                arguments={
                    "title": f"Autonomous Subtask {i}",
                    "description": f"Subtask {i} for autonomous mode testing",
                    "task_type": "task",
                    "parent_task_id": epic_id,
                    "category": "code",
                    "implementation_domain": "backend",
                    "validation_criteria": "Tests pass and task is functional",
                },
            )
            result = unwrap_result(raw_result)
            assert result.get("id") is not None, f"Subtask {i} creation failed: {result}"
            subtask_ids.append(result["id"])

        # Verify tasks are ready (no blockers)
        raw_result = mcp_client.call_tool(
            server_name="gobby-tasks",
            tool_name="list_ready_tasks",
            arguments={"parent_task_id": epic_id},
        )
        result = unwrap_result(raw_result)
        ready_tasks = result.get("tasks", [])

        # Both subtasks should be ready
        ready_ids = [t["id"] for t in ready_tasks]
        for subtask_id in subtask_ids:
            assert subtask_id in ready_ids, f"Subtask {subtask_id} should be ready"
        # Gate check passes: ready tasks exist and are eligible for scheduling

    def test_suggest_next_task_returns_ready_task(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test that suggest_next_task returns a task when ready tasks exist."""
        # Setup
        project_result = cli_events.register_test_project(
            project_id="e2e-test-project",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        session_external_id = f"suggest-next-{uuid.uuid4().hex[:8]}"
        session_result = cli_events.register_session(
            external_id=session_external_id,
            machine_id="test-machine",
            source="Claude Code",
            cwd=str(daemon_instance.project_dir),
        )
        session_id = session_result["id"]
        mcp_client.session_id = session_id

        # Create a single task
        raw_result = mcp_client.call_tool(
            server_name="gobby-tasks",
            tool_name="create_task",
            arguments={
                "title": "Task for Suggestion Test",
                "task_type": "task",
                "category": "code",
                "implementation_domain": "backend",
                "validation_criteria": "Tests pass and task is functional",
            },
        )
        result = unwrap_result(raw_result)
        assert result.get("id") is not None, f"Task creation failed: {result}"
        task_id = result["id"]

        # Get suggestion
        raw_result = mcp_client.call_tool(
            server_name="gobby-tasks",
            tool_name="suggest_next_task",
            arguments={"session_id": session_id},
        )
        result = unwrap_result(raw_result)

        # Should suggest a task - suggest_next_task returns 'suggestion' key, not 'success'
        assert result.get("suggestion") is not None, (
            f"suggest_next_task returned no suggestion: {result}"
        )
        suggestion = result["suggestion"]
        assert "ref" in suggestion or "id" in suggestion, (
            f"Suggestion should have task info: {suggestion}"
        )

        # Verify the suggestion refers to the task we created
        suggested_id = None
        if "id" in suggestion:
            suggested_id = suggestion["id"]
        elif "ref" in suggestion:
            ref = suggestion["ref"]
            if isinstance(ref, str):
                suggested_id = ref
            elif isinstance(ref, dict) and "id" in ref:
                suggested_id = ref["id"]

        assert suggested_id == task_id, (
            f"Suggestion should refer to created task {task_id}, "
            f"but got {suggested_id}. Full suggestion: {suggestion}"
        )
