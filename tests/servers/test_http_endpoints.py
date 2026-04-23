"""HTTP server endpoint coverage tests."""

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.servers.http import HTTPServer
from gobby.storage.database import LocalDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


@pytest.mark.integration
class TestAdminEndpoints:
    """Additional tests for admin endpoints."""

    def test_status_check_running_true(self, client: TestClient) -> None:
        """Test status check when server is running."""
        response = client.get("/api/admin/status")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ["healthy", "degraded", "ok"]

    def test_status_check_with_daemon(self, basic_http_server: HTTPServer) -> None:
        """Test status check includes daemon status when available."""
        mock_daemon = MagicMock()
        mock_daemon.status.return_value = {"state": "running", "uptime": 100}
        basic_http_server._daemon = mock_daemon

        client = TestClient(basic_http_server.app)
        response = client.get("/api/admin/status")

        assert response.status_code == 200
        data = response.json()
        assert data["daemon"] == {"state": "running", "uptime": 100}

    def test_status_check_daemon_status_failure(self, basic_http_server: HTTPServer) -> None:
        """Test status check handles daemon status failure."""
        mock_daemon = MagicMock()
        mock_daemon.status.side_effect = RuntimeError("Daemon error")
        basic_http_server._daemon = mock_daemon

        client = TestClient(basic_http_server.app)
        response = client.get("/api/admin/status")

        assert response.status_code == 200
        data = response.json()
        assert data["daemon"] is None

    def test_status_check_with_task_manager(
        self, session_storage: SessionManager, temp_db: LocalDatabase
    ) -> None:
        """Test status check includes task stats."""
        from gobby.storage.tasks import LocalTaskManager

        task_manager = LocalTaskManager(temp_db)

        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=task_manager,
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

        client = TestClient(server.app)
        response = client.get("/api/admin/status")

        assert response.status_code == 200
        data = response.json()
        assert "tasks" in data
        assert "open" in data["tasks"]
        assert "in_progress" in data["tasks"]

    def test_status_check_with_memory_manager(
        self, session_storage: SessionManager, temp_db: LocalDatabase
    ) -> None:
        """Test status check includes memory stats."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.get_stats.return_value = {
            "total_count": 10,
        }

        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
            memory_manager=mock_memory_manager,
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

        client = TestClient(server.app)
        response = client.get("/api/admin/status")

        assert response.status_code == 200
        data = response.json()
        assert data["memory"]["count"] == 10

    def test_status_check_memory_manager_failure(self, session_storage: SessionManager) -> None:
        """Test status check handles memory manager failure."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.get_stats.side_effect = RuntimeError("Memory error")

        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
            memory_manager=mock_memory_manager,
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

        client = TestClient(server.app)
        response = client.get("/api/admin/status")

        assert response.status_code == 200
        data = response.json()
        assert data["memory"]["count"] == 0

    def test_shutdown_creates_background_task(self, basic_http_server: HTTPServer) -> None:
        """Test shutdown endpoint creates background task."""
        client = TestClient(basic_http_server.app)

        response = client.post("/api/admin/shutdown")
        assert response.status_code == 200

        assert response.json()["status"] == "shutting_down"

    def test_metrics_endpoint_with_daemon(
        self, client: TestClient, basic_http_server: HTTPServer
    ) -> None:
        """Test metrics endpoint returns Prometheus format."""
        response = client.get("/api/admin/metrics")

        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        assert "# HELP" in response.text or "# TYPE" in response.text

    def test_config_endpoint_error_handling(self, session_storage: SessionManager) -> None:
        """Test config endpoint handles errors."""
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

        with patch("gobby.servers.routes.admin._config.get_version") as mock_version:
            mock_version.side_effect = RuntimeError("Version error")

            client = TestClient(server.app)
            response = client.get("/api/admin/config")

            assert response.status_code == 500


@pytest.mark.integration
class TestMCPEndpoints:
    """Tests for MCP endpoints."""

    @pytest.fixture
    def mcp_server(self, session_storage: SessionManager) -> HTTPServer:
        """Create server for MCP tests."""
        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        return HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

    @pytest.fixture
    def mcp_client(self, mcp_server: HTTPServer) -> Iterator[TestClient]:
        """Create test client that runs lifespan to set app.state.server."""
        with TestClient(mcp_server.app) as c:
            yield c

    def test_list_mcp_servers_empty(self, mcp_client: TestClient) -> None:
        """Test listing MCP servers when none configured."""
        response = mcp_client.get("/api/mcp/servers")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["connected"] == 0

    def test_get_mcp_status_empty(self, mcp_client: TestClient) -> None:
        """Test MCP status with no servers."""
        response = mcp_client.get("/api/mcp/status")
        assert response.status_code == 200
        data = response.json()
        assert data["total_servers"] == 0
        assert data["connected_servers"] == 0

    def test_call_tool_missing_fields(self, mcp_client: TestClient) -> None:
        """Test calling tool with missing required fields."""
        response = mcp_client.post(
            "/api/mcp/tools/call",
            json={"tool_name": "test-tool"},
        )
        assert response.status_code == 400
        assert "server_name" in response.json()["detail"]["error"]

    def test_get_tool_schema_missing_fields(self, mcp_client: TestClient) -> None:
        """Test getting tool schema with missing fields."""
        response = mcp_client.post(
            "/api/mcp/tools/schema",
            json={"server_name": "test-server"},
        )
        assert response.status_code == 400

    def test_recommend_tools_missing_task(self, mcp_client: TestClient) -> None:
        """Test recommend tools with missing task_description."""
        response = mcp_client.post(
            "/api/mcp/tools/recommend",
            json={"search_mode": "llm"},
        )
        assert response.status_code == 400
        assert "task_description" in response.json()["detail"]["error"]

    def test_search_tools_missing_query(self, mcp_client: TestClient) -> None:
        """Test search tools with missing query."""
        response = mcp_client.post(
            "/api/mcp/tools/search",
            json={},
        )
        assert response.status_code == 400
        assert "query" in response.json()["detail"]["error"]

    def test_proxy_invalid_json(self, mcp_client: TestClient) -> None:
        """Test MCP proxy with invalid JSON body."""
        response = mcp_client.post(
            "/api/mcp/test-server/tools/test-tool",
            content="not valid json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert "Invalid JSON" in response.json()["detail"]["error"]

    def test_add_server_missing_fields(self, mcp_client: TestClient) -> None:
        """Test adding server with missing required fields."""
        response = mcp_client.post(
            "/api/mcp/servers",
            json={"name": "test-server"},
        )
        assert response.status_code == 400
        assert "transport" in response.json()["detail"]["error"]

    def test_import_server_missing_source(self, mcp_client: TestClient) -> None:
        """Test import server with no source specified."""
        response = mcp_client.post(
            "/api/mcp/servers/import",
            json={},
        )
        assert response.status_code == 400
        assert "at least one" in response.json()["detail"]["error"]

    def test_list_tools_external_server_not_found(self, mcp_client: TestClient) -> None:
        """Test listing tools for unknown external server returns envelope error."""
        response = mcp_client.get("/api/mcp/unknown-server/tools")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "error" in data

    def test_mcp_tools_list_all(self, mcp_client: TestClient) -> None:
        """Test listing all MCP tools."""
        response = mcp_client.get("/api/mcp/tools")
        assert response.status_code == 200
        data = response.json()
        assert "tools" in data


class FakeMCPManagerSimple:
    """Simple fake MCP manager for testing without full initialization."""

    def __init__(self) -> None:
        self.server_configs: list[Any] = []
        self.connections: dict[str, Any] = {}
        self.health: dict[str, Any] = {}
        self._configs: dict[str, Any] = {}
        self.project_id = "test-project"

    def has_server(self, server_name: str) -> bool:
        return server_name in self._configs


@pytest.mark.integration
class TestMCPEndpointsWithManager:
    """Tests for MCP endpoints with mock MCP manager."""

    @pytest.fixture
    def http_server_with_mcp(
        self,
        session_storage: SessionManager,
    ) -> HTTPServer:
        """Create HTTP server and set mcp_manager after init to avoid GobbyDaemonTools."""
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
        server.mcp_manager = FakeMCPManagerSimple()
        server.services.mcp_manager = server.mcp_manager
        return server

    @pytest.fixture
    def mcp_client(self, http_server_with_mcp: HTTPServer) -> Iterator[TestClient]:
        """Create test client with MCP manager."""
        with TestClient(http_server_with_mcp.app) as c:
            yield c

    def test_remove_server_not_found(
        self, mcp_client: TestClient, http_server_with_mcp: HTTPServer
    ) -> None:
        """Test removing non-existent server returns envelope error."""
        http_server_with_mcp.mcp_manager.remove_server = AsyncMock(
            side_effect=ValueError("Server not found")
        )

        response = mcp_client.delete("/api/mcp/servers/nonexistent")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Server not found" in data["error"]

    def test_remove_server_success(
        self, mcp_client: TestClient, http_server_with_mcp: HTTPServer
    ) -> None:
        """Test removing server successfully."""
        http_server_with_mcp.mcp_manager.remove_server = AsyncMock()

        response = mcp_client.delete("/api/mcp/servers/test-server")
        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_list_all_tools_with_server_filter(
        self, mcp_client: TestClient, http_server_with_mcp: HTTPServer
    ) -> None:
        """Test listing tools with server filter."""
        response = mcp_client.get("/api/mcp/tools?server_filter=nonexistent")
        assert response.status_code == 200
        data = response.json()
        assert "tools" in data


@pytest.mark.integration
class TestCodeEndpoints:
    """Tests for code execution endpoints."""

    @pytest.fixture
    def code_server(self, session_storage: SessionManager) -> HTTPServer:
        """Create server for code endpoint tests."""
        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        return HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

    @pytest.fixture
    def code_client(self, code_server: HTTPServer) -> Iterator[TestClient]:
        """Create test client that runs lifespan to set app.state.server."""
        with TestClient(code_server.app) as c:
            yield c

    def test_execute_code_missing_code(self, code_client: TestClient) -> None:
        """Test execute_code endpoint was removed."""
        response = code_client.post(
            "/code/execute",
            json={"language": "python"},
        )
        assert response.status_code == 404

    def test_process_dataset_missing_data(self, code_client: TestClient) -> None:
        """Test process_dataset endpoint was removed."""
        response = code_client.post(
            "/code/process-dataset",
            json={"operation": "summarize"},
        )
        assert response.status_code == 404

    def test_process_dataset_missing_operation(self, code_client: TestClient) -> None:
        """Test process_dataset endpoint was removed."""
        response = code_client.post(
            "/code/process-dataset",
            json={"data": [1, 2, 3]},
        )
        assert response.status_code == 404


@pytest.mark.integration
class TestHooksEndpoints:
    """Tests for hooks endpoints."""

    def test_execute_hook_without_hook_manager(self, client: TestClient) -> None:
        """Test execute hook when hook manager not initialized."""
        if hasattr(client.app.state, "hook_manager"):
            del client.app.state.hook_manager

        response = client.post(
            "/api/hooks/execute",
            json={"hook_type": "session-start", "source": "claude"},
        )
        assert response.status_code == 503
        assert "HookManager not initialized" in response.json()["detail"]

    def test_execute_hook_with_mock_manager(self, session_storage: SessionManager) -> None:
        """Test execute hook with mocked hook manager."""
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

        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as MockAdapter:
            mock_adapter_instance = MagicMock()
            mock_adapter_instance.handle_native.return_value = {"continue": True}
            MockAdapter.return_value = mock_adapter_instance

            client = TestClient(server.app)
            response = client.post(
                "/api/hooks/execute",
                json={
                    "hook_type": "session-start",
                    "source": "claude",
                    "input_data": {},
                },
            )

            assert response.status_code == 200
            assert response.json()["continue"] is True

    def test_execute_hook_graceful_error_on_adapter_exception(
        self, session_storage: SessionManager
    ) -> None:
        """Hook adapter failures should return a non-fatal hook response."""
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

        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as MockAdapter:
            mock_adapter_instance = MagicMock()
            mock_adapter_instance.handle_native.side_effect = RuntimeError(
                "Database connection failed"
            )
            MockAdapter.return_value = mock_adapter_instance

            client = TestClient(server.app)
            response = client.post(
                "/api/hooks/execute",
                json={
                    "hook_type": "pre-tool-use",
                    "source": "claude",
                    "input_data": {"tool_name": "Read"},
                },
            )

            assert response.status_code == 200
            data = response.json()
            assert data["continue"] is True
            assert "hookSpecificOutput" in data
            assert data["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
            assert "non-fatal" in data["hookSpecificOutput"]["additionalContext"]
            assert "Database connection failed" in data["hookSpecificOutput"]["additionalContext"]

    def test_execute_hook_graceful_error_for_unsupported_hook_type(
        self, session_storage: SessionManager
    ) -> None:
        """Unsupported hook-type failures should still return continue=True."""
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

        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as MockAdapter:
            mock_adapter_instance = MagicMock()
            mock_adapter_instance.handle_native.side_effect = RuntimeError("Some error")
            MockAdapter.return_value = mock_adapter_instance

            client = TestClient(server.app)
            response = client.post(
                "/api/hooks/execute",
                json={
                    "hook_type": "session-start",
                    "source": "claude",
                    "input_data": {},
                },
            )

            assert response.status_code == 200
            data = response.json()
            assert data["continue"] is True
            assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"
            assert "non-fatal" in data["hookSpecificOutput"]["additionalContext"]
            assert "Some error" in data["hookSpecificOutput"]["additionalContext"]


@pytest.mark.integration
class TestWebhooksEndpoints:
    """Tests for webhooks endpoints."""

    @pytest.fixture
    def webhooks_server(self, session_storage: SessionManager) -> HTTPServer:
        """Create server for webhooks tests."""
        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        return HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

    @pytest.fixture
    def webhooks_client(self, webhooks_server: HTTPServer) -> Iterator[TestClient]:
        """Create test client that runs lifespan."""
        with TestClient(webhooks_server.app) as c:
            yield c

    def test_list_webhooks_no_config(self, webhooks_client: TestClient) -> None:
        """Test list webhooks when config is None."""
        response = webhooks_client.get("/api/webhooks")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["enabled"] is False
        assert data["endpoints"] == []

    def test_list_webhooks_endpoint_exists(self, session_storage: SessionManager) -> None:
        """Test webhooks endpoint works with minimal config."""
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

        with TestClient(server.app) as client:
            response = client.get("/api/webhooks")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["enabled"] is False
        assert data["endpoints"] == []

    def test_test_webhook_missing_name(self, webhooks_client: TestClient) -> None:
        """Test webhook test with missing name."""
        response = webhooks_client.post("/api/webhooks/test", json={})
        assert response.status_code == 400
        assert "Webhook name required" in response.json()["detail"]

    def test_test_webhook_no_config(self, webhooks_client: TestClient) -> None:
        """Test webhook test when config is None."""
        response = webhooks_client.post("/api/webhooks/test", json={"name": "test"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Configuration not available" in data["error"]


@pytest.mark.integration
class TestInternalRegistries:
    """Tests for internal registry handling."""

    def test_list_tools_internal_server(self, session_storage: SessionManager) -> None:
        """Test listing tools from internal server."""
        mock_internal_manager = MagicMock()
        mock_internal_manager.is_internal.return_value = True
        mock_internal_manager.get_all_registries.return_value = []
        mock_registry = MagicMock()
        mock_registry.list_tools.return_value = [{"name": "tool1", "description": "Test tool"}]
        mock_internal_manager.get_registry.return_value = mock_registry

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
        server._internal_manager = mock_internal_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/gobby-tasks/tools")

        assert response.status_code == 200
        data = response.json()
        assert data["tool_count"] == 1
        assert data["tools"][0]["name"] == "tool1"

    def test_list_tools_internal_server_not_found(self, session_storage: SessionManager) -> None:
        """Test listing tools from non-existent internal server."""
        mock_internal_manager = MagicMock()
        mock_internal_manager.is_internal.return_value = True
        mock_internal_manager.get_registry.return_value = None
        mock_internal_manager.get_all_registries.return_value = []

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
        server._internal_manager = mock_internal_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/gobby-nonexistent/tools")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not found" in data["error"]

    def test_call_tool_internal_server(self, session_storage: SessionManager) -> None:
        """Test calling tool on internal server."""
        mock_internal_manager = MagicMock()
        mock_internal_manager.is_internal.return_value = True
        mock_internal_manager.get_all_registries.return_value = []
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"result": "success"})
        mock_internal_manager.get_registry.return_value = mock_registry

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
        server._internal_manager = mock_internal_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/gobby-tasks/tools/list_tasks",
                json={"status": "open"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["result"] == {"result": "success"}

    def test_call_tool_internal_server_error(self, session_storage: SessionManager) -> None:
        """Test calling tool on internal server with error."""
        mock_internal_manager = MagicMock()
        mock_internal_manager.is_internal.return_value = True
        mock_internal_manager.get_all_registries.return_value = []
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(side_effect=ValueError("Tool error"))
        mock_internal_manager.get_registry.return_value = mock_registry

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
        server._internal_manager = mock_internal_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/gobby-tasks/tools/failing_tool",
                json={},
            )

        assert response.status_code == 500

    def test_get_tool_schema_internal_server(self, session_storage: SessionManager) -> None:
        """Test getting tool schema from internal server."""
        mock_internal_manager = MagicMock()
        mock_internal_manager.is_internal.return_value = True
        mock_internal_manager.get_all_registries.return_value = []
        mock_registry = MagicMock()
        mock_registry.get_schema.return_value = {
            "type": "object",
            "properties": {"status": {"type": "string"}},
        }
        mock_internal_manager.get_registry.return_value = mock_registry

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
        server._internal_manager = mock_internal_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/schema",
                json={"server_name": "gobby-tasks", "tool_name": "list_tasks"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "list_tasks"
        assert "inputSchema" in data
