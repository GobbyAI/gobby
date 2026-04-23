"""Tests for the HTTP server endpoints."""

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.servers.http import HTTPServer
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class TestAdminEndpoints:
    """Tests for admin endpoints."""

    def test_status_check(self, client: TestClient) -> None:
        """Test /admin/status endpoint returns health info."""
        response = client.get("/api/admin/status")
        assert response.status_code == 200

        data = response.json()
        assert "status" in data
        assert "server" in data
        assert "port" in data["server"]
        assert data["server"]["test_mode"] is True
        assert "provider_models" in data

    def test_config_endpoint(self, client: TestClient) -> None:
        """Test /admin/config endpoint returns configuration."""
        response = client.get("/api/admin/config")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "success"
        assert "config" in data
        assert "server" in data["config"]
        assert "endpoints" in data["config"]

    def test_metrics_endpoint(self, client: TestClient) -> None:
        """Test /admin/metrics endpoint returns Prometheus format."""
        response = client.get("/api/admin/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")


class TestStreamableHttpShutdown:
    """Tests for best-effort FastMCP streamable HTTP transport shutdown."""

    @pytest.mark.asyncio
    async def test_terminate_streamable_http_sessions_times_out_with_warning(
        self, http_server: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport = MagicMock()
        transport.mcp_session_id = "sess-timeout"
        transport.terminate = AsyncMock()
        http_server._mcp_server = MagicMock()
        http_server._mcp_server.session_manager = MagicMock(_server_instances={"one": transport})

        with patch("gobby.servers.http.asyncio.wait_for", side_effect=TimeoutError):
            await http_server._terminate_streamable_http_sessions()

        assert any(
            "Timed out terminating Streamable HTTP session sess-timeout" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_terminate_streamable_http_sessions_uses_wait_for(
        self, http_server: HTTPServer
    ) -> None:
        transport = MagicMock()
        transport.mcp_session_id = "sess-ok"
        transport.terminate = AsyncMock()
        http_server._mcp_server = MagicMock()
        http_server._mcp_server.session_manager = MagicMock(_server_instances={"one": transport})

        async def _wait_for(awaitable, timeout):
            await awaitable
            return None

        with patch(
            "gobby.servers.http.asyncio.wait_for",
            new=AsyncMock(side_effect=_wait_for),
        ) as mock_wait_for:
            await http_server._terminate_streamable_http_sessions()

        mock_wait_for.assert_awaited_once()
        assert mock_wait_for.await_args.kwargs["timeout"] == 2.0
        assert transport.terminate.await_count == 1


class TestHooksEndpoint:
    """Tests for hooks execution endpoint."""

    def test_execute_hook_missing_hook_type(self, client: TestClient) -> None:
        """Test hook execution with missing hook_type."""
        response = client.post(
            "/api/hooks/execute",
            json={"source": "claude"},
        )

        assert response.status_code == 400
        assert "hook_type" in response.json()["detail"]

    def test_execute_hook_missing_source(self, client: TestClient) -> None:
        """Test hook execution with missing source."""
        response = client.post(
            "/api/hooks/execute",
            json={"hook_type": "session-start"},
        )

        assert response.status_code == 400
        assert "source" in response.json()["detail"]

    def test_execute_hook_unsupported_source(self, client: TestClient) -> None:
        """Test hook execution with unsupported source returns error."""
        response = client.post(
            "/api/hooks/execute",
            json={
                "hook_type": "session-start",
                "source": "unsupported",
            },
        )

        # In test mode, HookManager may not be initialized (503) or source is invalid (400)
        assert response.status_code in [400, 503]


class TestMCPEndpoints:
    """Tests for MCP proxy endpoints."""

    def test_mcp_tools_without_manager(self, client: TestClient) -> None:
        """Test MCP tools listing when manager not available."""
        response = client.get("/api/mcp/test-server/tools")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "MCP manager not available" in data["error"]

    def test_mcp_proxy_without_manager(self, client: TestClient) -> None:
        """Test MCP proxy when manager not available."""
        response = client.post(
            "/api/mcp/test-server/tools/test-tool",
            json={},
        )
        assert response.status_code == 503


class FakeConnection:
    def __init__(self) -> None:
        self.is_connected = True
        self._session = MagicMock()
        self.config = MagicMock()
        self.config.transport = "stdio"
        self.config.project_id = "test-project"
        self.config.description = "Test Server"


class FakeMCPManager:
    def __init__(self) -> None:
        self.server_configs: list[Any] = []
        self.connections: dict[str, Any] = {}
        self.health: dict[str, Any] = {}
        self.get_client = MagicMock()
        self.call_tool = AsyncMock()
        self.project_id = "test-project"
        self.mcp_db_manager = None

    def has_server(self, server_name: str) -> bool:
        """Check if a server is configured."""
        return server_name in self.connections


class TestMCPEndpointsWithManager:
    """Tests for MCP endpoints with mock manager."""

    @pytest.fixture
    def mock_mcp_manager(self) -> FakeMCPManager:
        """Create a mock MCP manager."""

        return FakeMCPManager()

    @pytest.fixture
    def http_server_with_mcp(
        self,
        session_storage: SessionManager,
        mock_mcp_manager: FakeMCPManager,
    ) -> HTTPServer:
        """Create HTTP server with mock MCP manager."""
        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
            mcp_manager=mock_mcp_manager,
        )
        return HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

    @pytest.fixture
    def mcp_client(self, http_server_with_mcp: HTTPServer) -> Iterator[TestClient]:
        """Create test client with MCP manager."""
        with TestClient(http_server_with_mcp.app) as client:
            yield client

    def test_mcp_tools_server_not_found(
        self,
        mcp_client: TestClient,
        http_server_with_mcp: HTTPServer,
    ) -> None:
        """Test MCP tools listing for unknown server."""
        assert http_server_with_mcp.mcp_manager is not None
        http_server_with_mcp.mcp_manager.get_client.side_effect = ValueError("Server not found")

        # No try/except needed if we fixed the root cause, but leaving assertion
        response = mcp_client.get("/api/mcp/unknown-server/tools")
        assert response.status_code == 404

    def test_mcp_proxy_tool_not_found(
        self,
        mcp_client: TestClient,
        http_server_with_mcp: HTTPServer,
    ) -> None:
        """Test MCP proxy for unknown tool.

        Tool-level errors (tool not found, validation, execution) are returned as
        200 with error in response body. Only server-level errors return 404.
        See _process_tool_proxy_result in routes/mcp/tools.py.
        """
        assert http_server_with_mcp.mcp_manager is not None
        http_server_with_mcp.mcp_manager.call_tool = AsyncMock(
            side_effect=ValueError("Tool not found")
        )

        response = mcp_client.post(
            "/api/mcp/test-server/tools/unknown-tool",
            json={},
        )
        # Tool-level errors return 200 with error in body (application-level error)
        assert response.status_code == 200
        data = response.json()
        assert data.get("success") is False
        assert "Tool not found" in data.get("error", "")

    def test_add_mcp_server_success(
        self,
        mcp_client: TestClient,
        http_server_with_mcp: HTTPServer,
    ) -> None:
        """Test adding a new MCP server."""
        # Mock get_project_context
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "test-project-id", "name": "test"}
            assert http_server_with_mcp.mcp_manager is not None
            http_server_with_mcp.mcp_manager.add_server = AsyncMock()

            response = mcp_client.post(
                "/api/mcp/servers",
                json={
                    "name": "new-server",
                    "transport": "http",
                    "url": "http://example.com",
                    "enabled": True,
                },
            )

        assert response.status_code == 200
        assert response.json()["success"] is True

        # Verify add_server was called with correct config
        assert http_server_with_mcp.mcp_manager is not None
        http_server_with_mcp.mcp_manager.add_server.assert_called_once()
        config = http_server_with_mcp.mcp_manager.add_server.call_args[0][0]
        assert config.name == "new-server"
        assert config.project_id == "test-project-id"

    def test_add_mcp_server_no_project(
        self,
        mcp_client: TestClient,
        http_server_with_mcp: HTTPServer,
    ) -> None:
        """Test adding MCP server without project context fails."""
        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            response = mcp_client.post(
                "/api/mcp/servers",
                json={
                    "name": "new-server",
                    "transport": "http",
                    "url": "http://example.com",
                },
            )

        assert response.status_code == 400
        # HTTPException returns {"success": False, "error": "..."} in detail
        detail = response.json()["detail"]
        error_msg = detail.get("error", "") if isinstance(detail, dict) else str(detail)
        assert "No current project" in error_msg


class TestExceptionHandling:
    """Tests for exception handling."""

    def test_global_exception_returns_200(
        self,
        session_storage: SessionManager,
    ) -> None:
        """Test that global exception handler returns 200 to prevent hook failures."""
        # Create server that will raise an exception
        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

        # Mock to raise exception
        # Call an endpoint that uses session_storage.get (e.g. invalid status update which fetches session)
        # but here we mocked .get globally.
        # Let's use a simpler approach: define a route in the app that raises an exception
        @server.app.get("/trigger_error")
        def trigger_error() -> None:
            raise RuntimeError("Test error")

        client = TestClient(server.app, raise_server_exceptions=False)
        response = client.get("/trigger_error")

        # Should return 200 with error details in JSON (as per global handler logic for hooks/background)
        # OR 500 if it's a standard request.
        # Wait, the requirement says "verify global exception handler".
        # If the handler is for hooks, it traps exceptions.
        # If for standard HTTP, it likely returns 500.
        # Let's check what the global handler actually does.
        # Assuming it traps and logs, allowing the server to stay alive.

        # For this test, let's assume standard behavior (500) but ensuring app doesn't crash on outer loop.
        # Actually, if it's 500, that IS handled. Unhandled would crash uvicorn worker.
        # The global exception handler traps errors and returns 200 with error details
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "error"
        assert "message" in data


class TestShutdownEndpoint:
    """Tests for shutdown endpoint."""

    def test_shutdown_initiates(self, client: TestClient) -> None:
        """Test that shutdown endpoint initiates shutdown."""
        response = client.post("/api/admin/shutdown")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "shutting_down"
        assert "response_time_ms" in data
