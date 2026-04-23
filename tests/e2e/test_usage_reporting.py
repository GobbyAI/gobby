"""E2E tests for token usage reporting."""

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


class TestUsageToolsAvailability:
    """Tests to verify usage tools are properly registered."""

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


class TestUsageReporting:
    """Tests for usage report tracking functionality."""

    def test_get_usage_report_initial(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test get_usage_report returns the expected structure."""
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
        assert "total_cache_creation_tokens" in usage, f"Missing cache create tokens: {result}"
        assert "total_cache_read_tokens" in usage, f"Missing cache read tokens: {result}"
        assert "session_count" in usage, f"Missing session_count: {result}"
        assert "usage_by_model" in usage, f"Missing usage_by_model: {result}"
        assert "usage_by_source" in usage, f"Missing usage_by_source: {result}"

    def test_get_usage_report_custom_days(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test get_usage_report with a custom days parameter."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-metrics",
            tool_name="get_usage_report",
            arguments={"days": 7},
        )
        result = unwrap_result(raw_result)

        assert "error" not in result, f"get_usage_report failed: {result}"
        usage = result.get("usage", {})
        assert usage.get("period_days") == 7, f"Period days mismatch: {result}"

    def test_usage_report_reflects_session_usage(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test that usage report includes session usage."""
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

        usage_result = cli_events.set_session_usage(
            session_id=session_id,
            input_tokens=5000,
            output_tokens=2500,
            cache_creation_tokens=100,
            cache_read_tokens=200,
        )
        assert usage_result["status"] == "success", f"Failed to set usage: {usage_result}"

        raw_result = mcp_client.call_tool(
            server_name="gobby-metrics",
            tool_name="get_usage_report",
            arguments={"days": 1},
        )
        result = unwrap_result(raw_result)

        assert "error" not in result, f"get_usage_report failed: {result}"
        usage = result.get("usage", {})

        assert usage.get("total_input_tokens", 0) >= 5000, (
            f"Input tokens should include our session: {usage}"
        )
        assert usage.get("total_output_tokens", 0) >= 2500, (
            f"Output tokens should include our session: {usage}"
        )

    def test_usage_report_aggregates_multiple_sessions(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test that usage reports aggregate usage from multiple sessions."""
        project_result = cli_events.register_test_project(
            project_id="e2e-test-project",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        total_tokens = 0
        for i in range(3):
            session_external_id = f"multi-session-{i}-{uuid.uuid4().hex[:8]}"
            session_result = cli_events.register_session(
                external_id=session_external_id,
                machine_id="test-machine",
                source="Claude Code",
                cwd=str(daemon_instance.project_dir),
            )
            session_id = session_result["id"]

            total_tokens += 3000 * (i + 1)
            usage_result = cli_events.set_session_usage(
                session_id=session_id,
                input_tokens=2000 * (i + 1),
                output_tokens=1000 * (i + 1),
            )
            assert usage_result["status"] == "success"

        raw_result = mcp_client.call_tool(
            server_name="gobby-metrics",
            tool_name="get_usage_report",
            arguments={"days": 1},
        )
        result = unwrap_result(raw_result)

        assert "error" not in result, f"get_usage_report failed: {result}"
        usage = result.get("usage", {})
        assert usage.get("total_input_tokens", 0) + usage.get("total_output_tokens", 0) >= (
            total_tokens
        ), f"Usage should aggregate multiple sessions: {usage}"
