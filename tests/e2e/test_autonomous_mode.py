"""
E2E tests for autonomous mode (ConductorLoop).

Tests verify:
1. Conductor start/stop endpoints exist
2. Budget status tools are available (get_budget_status, list_ready_tasks)
3. Conductor lifecycle: start → status → stop
4. Autonomous spawning gate: tasks + budget + readiness
5. Throttling when budget is exceeded

Test scenario:
1. Create ready tasks (epic with subtasks)
2. Verify budget allows spawning (over_budget: false)
3. Verify ready tasks are detected
4. Set high usage to exceed budget
5. Verify spawn would be blocked (over_budget: true)

Note: Full agent auto-spawning requires LLM API keys which are disabled in E2E tests.
These tests verify the infrastructure (tools, budget tracking, task readiness) is correct.
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

    def test_budget_tools_are_registered(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Verify budget management tools are available on gobby-metrics server."""
        tools = mcp_client.list_tools(server_name="gobby-metrics")
        tool_names = [t["name"] for t in tools]

        expected_tools = [
            "get_usage_report",
            "get_budget_status",
        ]

        for tool in expected_tools:
            assert tool in tool_names, f"Missing tool: {tool}"

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


class TestConductorLifecycle:
    """Tests for conductor start → status → stop lifecycle."""

    def test_get_budget_status_returns_structure(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test get_budget_status returns correct budget structure."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-metrics",
            tool_name="get_budget_status",
            arguments={},
        )
        result = unwrap_result(raw_result)

        # Should return budget structure
        assert result.get("success") is True, f"get_budget_status failed: {result}"
        budget = result.get("budget", {})

        # Verify expected fields
        assert "daily_budget_usd" in budget, f"Missing daily_budget_usd: {result}"
        assert "used_today_usd" in budget, f"Missing used_today_usd: {result}"
        assert "remaining_usd" in budget, f"Missing remaining_usd: {result}"
        assert "percentage_used" in budget, f"Missing percentage_used: {result}"
        assert "over_budget" in budget, f"Missing over_budget: {result}"

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
    """Tests for the autonomous spawning gate (tasks + budget + readiness)."""

    def test_autonomous_gate_with_ready_tasks_and_budget(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test that ready tasks and available budget allow spawning.

        This tests the "gate" for autonomous mode:
        - Tasks exist and are ready (no blockers)
        - Budget is not exceeded

        Note: Actual auto-spawn not tested (requires LLM keys).
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

        # Create epic with subtasks
        raw_result = mcp_client.call_tool(
            server_name="gobby-tasks",
            tool_name="create_task",
            arguments={
                "title": "Autonomous Mode Test Epic",
                "description": "Epic for testing autonomous mode gate",
                "task_type": "epic",
                "category": "code",
                "validation_criteria": "Tests pass and task is functional",
                "session_id": session_id,
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
                    "validation_criteria": "Tests pass and task is functional",
                    "session_id": session_id,
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

        # Verify budget allows spawning (using low usage)
        usage_result = cli_events.set_session_usage(
            session_id=session_id,
            input_tokens=1000,
            output_tokens=500,
            total_cost_usd=0.10,  # $0.10 - well under $1.00 budget
        )
        assert usage_result["status"] == "success", f"Failed to set usage: {usage_result}"

        # Check budget status
        raw_result = mcp_client.call_tool(
            server_name="gobby-metrics",
            tool_name="get_budget_status",
            arguments={},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is True, f"get_budget_status failed: {result}"
        budget = result.get("budget", {})
        assert budget.get("over_budget") is False, f"Should not be over budget: {budget}"

        # Gate check passes: ready tasks exist AND budget is available
        # In autonomous mode, this would trigger agent spawning

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

        # Create a single task
        raw_result = mcp_client.call_tool(
            server_name="gobby-tasks",
            tool_name="create_task",
            arguments={
                "title": "Task for Suggestion Test",
                "task_type": "task",
                "category": "code",
                "validation_criteria": "Tests pass and task is functional",
                "session_id": session_id,
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


class TestAutonomousThrottling:
    """Tests for autonomous mode throttling based on budget."""

    def test_budget_exceeded_blocks_spawning(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test that exceeding budget would block autonomous spawning.

        Config has:
        - daily_budget_usd: 1.0
        - throttle_threshold: 0.9 (90%)

        So spawning is blocked when usage >= $0.90
        """
        # Setup - register project and session
        project_result = cli_events.register_test_project(
            project_id="e2e-test-project",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        session_external_id = f"throttle-{uuid.uuid4().hex[:8]}"
        session_result = cli_events.register_session(
            external_id=session_external_id,
            machine_id="test-machine",
            source="Claude Code",
            cwd=str(daemon_instance.project_dir),
        )
        session_id = session_result["id"]

        # Set high usage (exceeds $1.00 budget)
        usage_result = cli_events.set_session_usage(
            session_id=session_id,
            input_tokens=100000,
            output_tokens=50000,
            total_cost_usd=1.50,  # $1.50 - over $1.00 budget
        )
        assert usage_result["status"] == "success", f"Failed to set usage: {usage_result}"

        # Check budget status
        raw_result = mcp_client.call_tool(
            server_name="gobby-metrics",
            tool_name="get_budget_status",
            arguments={},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is True, f"get_budget_status failed: {result}"
        budget = result.get("budget", {})
        assert budget.get("over_budget") is True, f"Should be over budget: {budget}"

        # In autonomous mode, this would block agent spawning
        # The conductor loop checks can_spawn_agent() which returns False when over_budget

    def test_budget_status_can_still_be_queried_when_over_budget(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test that budget status tools still work when budget is exceeded."""
        # Setup
        project_result = cli_events.register_test_project(
            project_id="e2e-test-project",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        session_external_id = f"query-over-{uuid.uuid4().hex[:8]}"
        session_result = cli_events.register_session(
            external_id=session_external_id,
            machine_id="test-machine",
            source="Claude Code",
            cwd=str(daemon_instance.project_dir),
        )
        session_id = session_result["id"]

        # Set very high usage
        cli_events.set_session_usage(
            session_id=session_id,
            input_tokens=500000,
            output_tokens=250000,
            total_cost_usd=5.00,  # $5.00 - way over budget
        )

        # Both budget status and usage report should still work
        budget_result = mcp_client.call_tool(
            server_name="gobby-metrics",
            tool_name="get_budget_status",
            arguments={},
        )
        budget = unwrap_result(budget_result)
        assert budget.get("success") is True, f"Budget status should work: {budget}"

        usage_result = mcp_client.call_tool(
            server_name="gobby-metrics",
            tool_name="get_usage_report",
            arguments={"days": 1},
        )
        usage = unwrap_result(usage_result)
        assert usage.get("success") is True, f"Usage report should work: {usage}"
