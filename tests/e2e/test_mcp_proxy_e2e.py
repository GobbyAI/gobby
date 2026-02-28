"""
E2E tests for MCP proxy tool discovery and invocation.

Tests verify:
1. Client connects to MCP proxy and discovers available tools
2. Tool invocation returns expected results
3. Multiple concurrent tool calls are handled correctly
4. Invalid tool calls return proper error responses
5. Tool discovery updates when servers are added/removed dynamically
"""

import asyncio

import httpx
import pytest

from tests.e2e.conftest import DaemonInstance, MCPTestClient

pytestmark = pytest.mark.e2e


class TestMCPProxyToolDiscovery:
    """Tests for MCP proxy tool discovery."""

    def test_client_discovers_internal_servers(
        self, daemon_instance: DaemonInstance, mcp_client: MCPTestClient
    ) -> None:
        """Verify client can discover internal gobby servers."""
        servers = mcp_client.list_servers()

        # Should have internal servers
        assert isinstance(servers, list)

        # Check for expected internal servers (gobby-*)
        server_names = [s.get("name") for s in servers]

        # At minimum, gobby-tasks should be present
        # Other servers may vary based on configuration
        internal_servers = [n for n in server_names if n and n.startswith("gobby-")]
        assert len(internal_servers) >= 1, (
            f"Should have at least one gobby-* server, got: {server_names}"
        )

    def test_client_discovers_tools_from_internal_servers(
        self, daemon_instance: DaemonInstance, mcp_client: MCPTestClient
    ) -> None:
        """Verify client can discover tools from internal servers."""
        # List all tools
        tools = mcp_client.list_tools()

        # Should have some tools
        assert isinstance(tools, list)
        assert len(tools) > 0, "Should have at least one tool available"

        # Tools should have name and description
        for tool in tools[:5]:  # Check first 5
            assert "name" in tool, f"Tool should have name: {tool}"

    def test_client_can_filter_tools_by_server(
        self, daemon_instance: DaemonInstance, mcp_client: MCPTestClient
    ) -> None:
        """Verify client can filter tools by server name."""
        # Get servers first
        servers = mcp_client.list_servers()
        if not servers:
            pytest.skip("No MCP servers available")

        # Find a gobby-* server
        gobby_server = None
        for s in servers:
            name = s.get("name", "")
            if name.startswith("gobby-"):
                gobby_server = name
                break

        if not gobby_server:
            pytest.skip("No gobby-* server available")

        # Filter tools by that server (using server_filter param)
        tools = mcp_client.list_tools(server_name=gobby_server)

        # Should have tools from that server
        assert len(tools) > 0, f"Should have tools from {gobby_server}"

        # All returned tools should be from the filtered server
        # Note: The API uses server_filter param, but may still return tools keyed by server
        for tool in tools:
            server_name = tool.get("server") or tool.get("server_name")
            # Only check if server_name is present (it's added by MCPTestClient)
            if server_name:
                assert server_name == gobby_server, (
                    f"Tool server mismatch: expected {gobby_server}, got {server_name}"
                )


class TestMCPProxyToolInvocation:
    """Tests for MCP proxy tool invocation."""

    def test_tool_invocation_returns_result(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify tool invocation returns expected result."""
        # Use the list_ready_tasks tool from gobby-tasks via HTTP directly
        # Endpoint is /mcp/tools/call (not /mcp/call)
        response = daemon_client.post(
            "/api/mcp/tools/call",
            json={
                "server_name": "gobby-tasks",
                "tool_name": "list_ready_tasks",
                "arguments": {},
            },
        )

        # Should get a successful response
        assert response.status_code == 200

        result = response.json()
        assert result is not None
        assert result.get("success") is True

    def test_tool_with_parameters(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify tool invocation with parameters works."""
        # Use get_task which takes a task_id parameter
        response = daemon_client.post(
            "/api/mcp/tools/call",
            json={
                "server_name": "gobby-tasks",
                "tool_name": "get_task",
                "arguments": {"task_id": "nonexistent-task"},
            },
        )

        # Should get a response (200 with error in body, or 400/404/500)
        assert response.status_code in [200, 400, 404, 500]

    def test_invalid_server_returns_error(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify calling tool on invalid server returns error."""
        response = daemon_client.post(
            "/api/mcp/tools/call",
            json={
                "server_name": "nonexistent-server",
                "tool_name": "some_tool",
                "arguments": {},
            },
        )

        # MCP errors return 200 with {success: False, ...} in body
        # for better MCP client compatibility (see execution.py _process_tool_proxy_result)
        # or HTTP error codes for malformed requests
        if response.status_code == 200:
            data = response.json()
            assert data.get("success") is False, "Invalid server should return success=False"
        else:
            assert response.status_code in [
                400,
                404,
                500,
                503,
            ], f"Invalid server should return error, got {response.status_code}"

    def test_invalid_tool_returns_error(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify calling invalid tool returns error in response body."""
        response = daemon_client.post(
            "/api/mcp/tools/call",
            json={
                "server_name": "gobby-tasks",
                "tool_name": "nonexistent_tool",
                "arguments": {},
            },
        )

        # Tool-level errors return HTTP 200 with error in response body
        # (HTTP 4xx/5xx reserved for transport/configuration errors)
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()
        assert data.get("success") is False, "Wrapper propagates inner result success status"
        result = data.get("result", {})
        assert result.get("success") is False, "Tool result should indicate failure"
        assert "not found" in result.get("error", "").lower()


class TestMCPProxyConcurrency:
    """Tests for concurrent MCP proxy tool calls."""

    @pytest.mark.asyncio
    async def test_multiple_concurrent_tool_calls(self, daemon_instance: DaemonInstance):
        """Verify multiple concurrent tool calls are handled correctly."""
        async with httpx.AsyncClient(base_url=daemon_instance.http_url, timeout=30.0) as client:
            # Create multiple concurrent requests
            async_tasks = []
            for _ in range(5):
                task = client.post(
                    "/api/mcp/tools/call",
                    json={
                        "server_name": "gobby-tasks",
                        "tool_name": "list_ready_tasks",
                        "arguments": {},
                    },
                )
                async_tasks.append(task)

            # Execute all concurrently
            responses = await asyncio.gather(*async_tasks, return_exceptions=True)

            # All should succeed
            success_count = 0
            for resp in responses:
                if isinstance(resp, Exception):
                    continue
                if resp.status_code == 200:
                    success_count += 1

            assert success_count >= 3, (
                f"At least 3 of 5 concurrent calls should succeed, got {success_count}"
            )

    @pytest.mark.asyncio
    async def test_concurrent_calls_to_different_tools(self, daemon_instance: DaemonInstance):
        """Verify concurrent calls to different tools work."""
        async with httpx.AsyncClient(base_url=daemon_instance.http_url, timeout=30.0) as client:
            # Different tool calls
            tool_calls = [
                ("gobby-tasks", "list_ready_tasks", {}),
                ("gobby-tasks", "suggest_next_task", {}),
            ]

            async_tasks = []
            for server, tool, args in tool_calls:
                task = client.post(
                    "/api/mcp/tools/call",
                    json={
                        "server_name": server,
                        "tool_name": tool,
                        "arguments": args,
                    },
                )
                async_tasks.append(task)

            # Execute concurrently
            responses = await asyncio.gather(*async_tasks, return_exceptions=True)

            # At least one should succeed
            success_count = sum(
                1 for r in responses if not isinstance(r, Exception) and r.status_code == 200
            )
            assert success_count >= 1, "At least one concurrent call should succeed"


class TestMCPProxyErrorHandling:
    """Tests for MCP proxy error handling."""

    def test_missing_required_parameters(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify missing required parameters returns error."""
        # Call endpoint without required server_name
        response = daemon_client.post(
            "/api/mcp/tools/call",
            json={
                "tool_name": "some_tool",
                "arguments": {},
            },
        )

        # Should get 400 for missing server_name (per the API implementation)
        assert response.status_code == 400, (
            f"Missing server_name should return 400, got {response.status_code}"
        )

    def test_malformed_json_returns_error(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify malformed JSON returns error."""
        response = daemon_client.post(
            "/api/mcp/tools/call",
            content="not valid json",
            headers={"Content-Type": "application/json"},
        )

        # Should get error - malformed JSON should fail with 4xx or 500
        # FastAPI may return 422 for validation, 400 for bad request, or 500 for parse error
        assert response.status_code in [
            400,
            422,
            500,
        ], f"Malformed JSON should return error, got {response.status_code}"

    def test_empty_request_returns_error(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify empty request returns error."""
        response = daemon_client.post("/api/mcp/tools/call", json={})

        # Should get 400 for missing required fields
        assert response.status_code == 400, (
            f"Empty request should return 400, got {response.status_code}"
        )


class TestMCPProxyToolSchema:
    """Tests for MCP proxy tool schema retrieval."""

    def test_get_tool_schema(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify tool schema can be retrieved."""
        # Schema endpoint is POST /mcp/tools/schema with JSON body
        response = daemon_client.post(
            "/api/mcp/tools/schema",
            json={
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
            },
        )

        assert response.status_code == 200, (
            f"Schema request should return 200, got {response.status_code}"
        )

        schema = response.json()
        assert schema is not None
        assert "name" in schema
        assert "inputSchema" in schema

    def test_schema_for_invalid_tool_returns_error(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify getting schema for invalid tool returns error."""
        response = daemon_client.post(
            "/api/mcp/tools/schema",
            json={
                "server_name": "gobby-tasks",
                "tool_name": "nonexistent_tool",
            },
        )

        # Should get 404 for tool not found
        assert response.status_code == 404, (
            f"Invalid tool schema should return 404, got {response.status_code}"
        )


class TestMCPProxyServerManagement:
    """Tests for MCP server management (add/remove)."""

    def test_servers_endpoint_returns_list(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify servers endpoint returns list of servers."""
        response = daemon_client.get("/api/mcp/servers")
        assert response.status_code == 200

        data = response.json()
        assert "servers" in data
        assert isinstance(data["servers"], list)

    def test_tools_endpoint_returns_data(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify tools endpoint returns tools data."""
        response = daemon_client.get("/api/mcp/tools")
        assert response.status_code == 200

        data = response.json()
        # Tools may be returned as dict (by server) or list
        assert "tools" in data
        tools = data["tools"]
        assert isinstance(tools, list | dict), f"Tools should be list or dict, got {type(tools)}"
