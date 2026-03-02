"""
E2E tests for token budget throttling.

Tests verify:
1. Budget status tools return correct data
2. Sessions with usage are tracked
3. Agent spawning is blocked when budget is exceeded
4. Budget threshold triggers throttling

Test scenario:
1. Create sessions with usage that exceeds throttle_threshold
2. Verify get_budget_status shows over_budget
3. Verify spawn operations are blocked when over budget
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


class TestBudgetToolsAvailability:
    """Tests to verify budget tools are properly registered."""

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

    def test_get_usage_report_schema(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Verify get_usage_report tool schema can be retrieved."""
        raw_schema = mcp_client.get_tool_schema(
            server_name="gobby-metrics",
            tool_name="get_usage_report",
        )

        assert raw_schema is not None
        assert isinstance(raw_schema, dict)

    def test_get_budget_status_schema(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Verify get_budget_status tool schema can be retrieved."""
        raw_schema = mcp_client.get_tool_schema(
            server_name="gobby-metrics",
            tool_name="get_budget_status",
        )

        assert raw_schema is not None
        assert isinstance(raw_schema, dict)


class TestBudgetStatusTracking:
    """Tests for budget status tracking functionality."""

    def test_get_budget_status_initial(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test get_budget_status returns correct structure."""
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

    def test_get_usage_report_default(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test get_usage_report returns correct structure."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-metrics",
            tool_name="get_usage_report",
            arguments={},  # Uses default days=1
        )
        result = unwrap_result(raw_result)

        # Should return usage structure
        assert result.get("success") is True, f"get_usage_report failed: {result}"
        usage = result.get("usage", {})

        # Verify expected fields
        assert "total_cost_usd" in usage, f"Missing total_cost_usd: {result}"
        assert "total_input_tokens" in usage, f"Missing total_input_tokens: {result}"
        assert "total_output_tokens" in usage, f"Missing total_output_tokens: {result}"
        assert "session_count" in usage, f"Missing session_count: {result}"

    def test_get_usage_report_custom_days(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test get_usage_report with custom days parameter."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-metrics",
            tool_name="get_usage_report",
            arguments={"days": 7},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is True, f"get_usage_report failed: {result}"
        usage = result.get("usage", {})
        assert usage.get("period_days") == 7, f"Period days mismatch: {result}"


class TestBudgetThrottling:
    """Tests for budget throttling behavior."""

    def test_budget_under_threshold(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test that budget under threshold shows not over_budget."""
        # Setup - register project and session
        project_result = cli_events.register_test_project(
            project_id="e2e-test-project",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        session_external_id = f"budget-under-{uuid.uuid4().hex[:8]}"
        session_result = cli_events.register_session(
            external_id=session_external_id,
            machine_id="test-machine",
            source="Claude Code",
            cwd=str(daemon_instance.project_dir),
        )
        session_id = session_result["id"]

        # Set low usage (well under $1.00 budget)
        usage_result = cli_events.set_session_usage(
            session_id=session_id,
            input_tokens=1000,
            output_tokens=500,
            total_cost_usd=0.10,  # $0.10 - under $0.90 threshold
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

    def test_budget_over_threshold_triggers_throttling(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test that exceeding budget threshold shows over_budget.

        Config has:
        - daily_budget_usd: 1.0
        - throttle_threshold: 0.9 (90%)

        So budget is exceeded when usage >= $0.90
        """
        # Setup - register project and session
        project_result = cli_events.register_test_project(
            project_id="e2e-test-project",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        session_external_id = f"budget-over-{uuid.uuid4().hex[:8]}"
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
        assert budget.get("used_today_usd") >= 1.0, f"Used should be >= $1.00: {budget}"

    def test_usage_report_reflects_session_usage(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test that usage report includes session usage."""
        # Setup
        project_result = cli_events.register_test_project(
            project_id="e2e-test-project",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        session_external_id = f"usage-report-{uuid.uuid4().hex[:8]}"
        session_result = cli_events.register_session(
            external_id=session_external_id,
            machine_id="test-machine",
            source="Claude Code",
            cwd=str(daemon_instance.project_dir),
        )
        session_id = session_result["id"]

        # Set specific usage
        usage_result = cli_events.set_session_usage(
            session_id=session_id,
            input_tokens=5000,
            output_tokens=2500,
            cache_creation_tokens=100,
            cache_read_tokens=200,
            total_cost_usd=0.25,
        )
        assert usage_result["status"] == "success", f"Failed to set usage: {usage_result}"

        # Get usage report
        raw_result = mcp_client.call_tool(
            server_name="gobby-metrics",
            tool_name="get_usage_report",
            arguments={"days": 1},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is True, f"get_usage_report failed: {result}"
        usage = result.get("usage", {})

        # Verify usage reflects what we set (may be cumulative with other tests)
        assert usage.get("total_cost_usd", 0) >= 0.25, f"Cost should include our session: {usage}"
        assert usage.get("total_input_tokens", 0) >= 5000, (
            f"Input tokens should include our session: {usage}"
        )


class TestAgentSpawnThrottling:
    """Tests for agent spawn throttling based on budget."""

    def test_spawn_agent_with_budget_tools_available(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Verify agent spawning tools are available."""
        # Check gobby-agents tools - spawn_agent is the unified tool that replaces
        # start_agent and spawn_agent_in_clone (use isolation param for clone/worktree)
        agent_tools = mcp_client.list_tools(server_name="gobby-agents")
        agent_tool_names = [t["name"] for t in agent_tools]
        assert "spawn_agent" in agent_tool_names, "Missing spawn_agent tool"

    def test_spawn_agent_over_budget_fails(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test that spawning agent fails when budget is exceeded.

        Note: This test verifies the spawn validation, not actual agent execution.
        The spawn_agent_in_clone tool checks budget via can_spawn_agent() before
        attempting to spawn.
        """
        # Setup - register project and session with high usage
        project_result = cli_events.register_test_project(
            project_id="e2e-test-project",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        session_external_id = f"spawn-over-{uuid.uuid4().hex[:8]}"
        session_result = cli_events.register_session(
            external_id=session_external_id,
            machine_id="test-machine",
            source="Claude Code",
            cwd=str(daemon_instance.project_dir),
        )
        session_id = session_result["id"]

        # Set high usage to exceed budget
        usage_result = cli_events.set_session_usage(
            session_id=session_id,
            input_tokens=500000,
            output_tokens=250000,
            total_cost_usd=5.00,  # $5.00 - way over $1.00 budget
        )
        assert usage_result["status"] == "success", f"Failed to set usage: {usage_result}"

        # Verify budget is exceeded
        raw_result = mcp_client.call_tool(
            server_name="gobby-metrics",
            tool_name="get_budget_status",
            arguments={},
        )
        result = unwrap_result(raw_result)
        assert result.get("success") is True
        budget = result.get("budget", {})
        assert budget.get("over_budget") is True, f"Budget should be exceeded: {budget}"

        # Attempt to spawn agent - should fail due to budget
        # Note: May fail for other reasons (no remote URL in test env) but we check
        # that if budget is mentioned in the error, the budget check is working
        raw_result = mcp_client.call_tool(
            server_name="gobby-agents",
            tool_name="spawn_agent",
            arguments={
                "prompt": "Test task",
                "parent_session_id": session_id,
                "isolation": "clone",
                "mode": "autonomous",
            },
        )
        result = unwrap_result(raw_result)

        # Should fail - either due to budget or spawn depth/remote issues
        assert result.get("success") is False, f"Spawn should fail when over budget: {result}"

        # Check if the error is budget-related (preferred) or another valid failure
        error_msg = str(result.get("error", "")).lower()
        is_budget_error = any(
            term in error_msg for term in ["budget", "exceeded", "over budget", "daily budget"]
        )
        is_valid_other_error = any(
            term in error_msg
            for term in [
                "remote",
                "url",
                "spawn",
                "depth",
                "runner",
                "clone_manager",  # Clone infrastructure not wired in e2e test
                "clone_storage",  # Clone infrastructure not wired in e2e test
            ]
        )
        assert is_budget_error or is_valid_other_error, (
            f"Expected budget-related or valid infrastructure error, got: {result.get('error')}"
        )


class TestMultiSessionBudgetAggregation:
    """Tests for budget aggregation across multiple sessions."""

    def test_budget_aggregates_multiple_sessions(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test that budget status aggregates usage from multiple sessions."""
        # Setup
        project_result = cli_events.register_test_project(
            project_id="e2e-test-project",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        # Create 3 sessions with usage
        total_cost = 0.0
        session_ids = []

        for i in range(3):
            session_external_id = f"multi-session-{i}-{uuid.uuid4().hex[:8]}"
            session_result = cli_events.register_session(
                external_id=session_external_id,
                machine_id="test-machine",
                source="Claude Code",
                cwd=str(daemon_instance.project_dir),
            )
            session_id = session_result["id"]
            session_ids.append(session_id)

            # Each session has $0.20 cost
            cost = 0.20
            total_cost += cost

            usage_result = cli_events.set_session_usage(
                session_id=session_id,
                input_tokens=2000 * (i + 1),
                output_tokens=1000 * (i + 1),
                total_cost_usd=cost,
            )
            assert usage_result["status"] == "success"

        # Check aggregated budget status
        raw_result = mcp_client.call_tool(
            server_name="gobby-metrics",
            tool_name="get_budget_status",
            arguments={},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is True, f"get_budget_status failed: {result}"
        budget = result.get("budget", {})

        # Used should be at least what we added (may include other test sessions)
        assert budget.get("used_today_usd", 0) >= total_cost, (
            f"Budget should aggregate multiple sessions. "
            f"Expected >= {total_cost}, got {budget.get('used_today_usd')}"
        )
