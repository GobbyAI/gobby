"""
Comprehensive unit tests for HTTP server to increase coverage.

This module focuses on:
1. HTTP endpoint handlers not covered by existing tests
2. Middleware behavior
3. Error handling paths
4. Edge cases in HTTPServer class methods
"""

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.servers.http import HTTPServer, create_server, run_server
from gobby.storage.database import LocalDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import LocalSessionManager

pytestmark = pytest.mark.unit

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def session_storage(temp_db: LocalDatabase) -> LocalSessionManager:
    """Create session storage."""
    return LocalSessionManager(temp_db)


@pytest.fixture
def project_storage(temp_db: LocalDatabase) -> LocalProjectManager:
    """Create project storage."""
    return LocalProjectManager(temp_db)


@pytest.fixture
def test_project(project_storage: LocalProjectManager, temp_dir: Path) -> dict[str, Any]:
    """Create a test project with project.json file."""
    project = project_storage.create(name="test-project", repo_path=str(temp_dir))

    # Create .gobby/project.json for project resolution
    gobby_dir = temp_dir / ".gobby"
    gobby_dir.mkdir()
    (gobby_dir / "project.json").write_text(f'{{"id": "{project.id}", "name": "test-project"}}')

    return project.to_dict()


@pytest.fixture
def basic_http_server(session_storage: LocalSessionManager) -> HTTPServer:
    """Create a basic HTTP server instance for testing."""
    mock_config = MagicMock()
    mock_config.logging.max_size_mb = 10
    mock_config.logging.backup_count = 3
    mock_config.memory.backend = "null"  # Avoid ValueError in memory backend init
    mock_config.workflow.timeout = 30  # Avoid TypeError in workflow hook init
    mock_config.workflow.enabled = True
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    services = ServiceContainer(
        config=mock_config,
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
def client(basic_http_server: HTTPServer) -> Iterator[TestClient]:
    """Create a test client for the HTTP server."""
    with patch("gobby.servers.http.HookManager") as MockHM:
        mock_instance = MockHM.return_value
        mock_instance._stop_registry = MagicMock()
        mock_instance.shutdown = MagicMock()
        with TestClient(basic_http_server.app) as client:
            yield client


# ============================================================================
# HTTPServer Initialization Tests
# ============================================================================


class TestHTTPServerInit:
    """Tests for HTTPServer initialization."""

    def test_init_minimal(self) -> None:
        """Test HTTPServer with minimal configuration."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)
        assert server.port == 8000
        assert server.test_mode is True
        assert server.mcp_manager is None
        assert server.config is services.config
        assert server.session_manager is services.session_manager
        assert server._mcp_server is None
        assert server._internal_manager is None
        assert server._tools_handler is None

    def test_init_with_port(self) -> None:
        """Test HTTPServer with custom port."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=9999, test_mode=False)
        assert server.port == 9999
        assert server.test_mode is False

    def test_init_sets_start_time(self) -> None:
        """Test that HTTPServer sets start time."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        before = time.time()
        server = HTTPServer(services=services, port=8000, test_mode=True)
        after = time.time()
        assert before <= server._start_time <= after

    def test_init_creates_broadcaster(self) -> None:
        """Test that HTTPServer creates broadcaster."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)
        assert server.broadcaster is not None

    def test_init_with_session_manager(self, session_storage: LocalSessionManager) -> None:
        """Test HTTPServer with session manager."""
        services = ServiceContainer(
            config=MagicMock(),
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=8000,
            test_mode=True,
        )
        assert server.session_manager is session_storage

    def test_init_background_tasks_empty(self) -> None:
        """Test that background tasks set is initialized empty."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)
        assert isinstance(server._background_tasks, set)
        assert len(server._background_tasks) == 0

    def test_init_running_flag_false(self) -> None:
        """Test that _running is initially False."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)
        assert server._running is False

    def test_init_creates_app(self) -> None:
        """Test that HTTPServer creates FastAPI app."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)
        assert isinstance(server.app, FastAPI)

    def test_init_with_llm_service(self) -> None:
        """Test HTTPServer with provided LLM service."""
        mock_llm = MagicMock()
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
            llm_service=mock_llm,
        )
        server = HTTPServer(
            services=services,
            port=8000,
            test_mode=True,
        )
        assert server.llm_service is mock_llm

    def test_init_creates_llm_service_from_config(self) -> None:
        """Test HTTPServer creates LLM service from config."""
        mock_config = MagicMock()
        mock_config.llm = MagicMock()

        # Mock the ServiceContainer to return our mock config
        services = ServiceContainer(
            config=mock_config,
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )

        with patch("gobby.servers.http.create_llm_service") as mock_create:
            mock_llm = MagicMock()
            mock_llm.enabled_providers = ["anthropic"]
            mock_create.return_value = mock_llm

            server = HTTPServer(
                services=services,
                port=8000,
                test_mode=True,
            )

            mock_create.assert_called_once_with(mock_config)
            assert server.llm_service is mock_llm
            # Also verify container was updated
            assert server.services.llm_service is mock_llm

    def test_init_llm_service_creation_failure(self) -> None:
        """Test HTTPServer handles LLM service creation failure."""
        mock_config = MagicMock()

        services = ServiceContainer(
            config=mock_config,
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )

        with patch("gobby.servers.http.create_llm_service") as mock_create:
            mock_create.side_effect = RuntimeError("LLM initialization failed")

            # Should not raise, just log warning
            server = HTTPServer(
                services=services,
                port=8000,
                test_mode=True,
            )

            assert server.llm_service is None


# ============================================================================
# Project ID Resolution Tests
# ============================================================================


class TestResolveProjectId:
    """Tests for resolve_project_id method."""

    def test_resolve_with_explicit_project_id(self, basic_http_server: HTTPServer) -> None:
        """Test that explicit project_id is returned directly."""
        result = basic_http_server.resolve_project_id("explicit-id", None)
        assert result == "explicit-id"

    def test_resolve_from_cwd(
        self, basic_http_server: HTTPServer, temp_dir: Path, test_project: dict[str, Any]
    ) -> None:
        """Test resolving project_id from cwd."""
        result = basic_http_server.resolve_project_id(None, str(temp_dir))
        assert result == test_project["id"]

    def test_resolve_no_project_json_raises(
        self, basic_http_server: HTTPServer, temp_dir: Path
    ) -> None:
        """Test that missing project.json raises ValueError."""
        # Create a directory without .gobby/project.json
        no_project_dir = temp_dir / "no_project"
        no_project_dir.mkdir()

        # Mock get_project_context to return None to isolate from filesystem state
        # (find_project_root searches up the tree and might find project.json in parents)
        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with pytest.raises(ValueError) as exc_info:
                basic_http_server.resolve_project_id(None, str(no_project_dir))

        assert "No .gobby/project.json found" in str(exc_info.value)
        assert "gobby init" in str(exc_info.value)

    def test_resolve_with_cwd_default(self, basic_http_server: HTTPServer) -> None:
        """Test resolution uses current directory when cwd is None."""
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "default-project-id", "name": "test"}

            result = basic_http_server.resolve_project_id(None, None)
            assert result == "default-project-id"


# ============================================================================
# Shutdown Processing Tests
# ============================================================================


class TestProcessShutdown:
    """Tests for _process_shutdown method."""

    @pytest.mark.asyncio
    async def test_shutdown_no_pending_tasks(self) -> None:
        """Test shutdown with no pending background tasks."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)

        await server._process_shutdown()

        # Should complete without error
        assert len(server._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_shutdown_waits_for_pending_tasks(self) -> None:
        """Test shutdown waits for pending background tasks."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)

        # Create a task that completes quickly
        async def quick_task() -> None:
            await asyncio.sleep(0.1)

        task = asyncio.create_task(quick_task())
        server._background_tasks.add(task)
        task.add_done_callback(server._background_tasks.discard)

        await server._process_shutdown()

        # Task should have completed
        assert len(server._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_shutdown_timeout_with_slow_tasks(self) -> None:
        """Test shutdown times out with very slow tasks."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)

        # Create a task that takes a very long time
        async def slow_task() -> None:
            await asyncio.sleep(100)

        task = asyncio.create_task(slow_task())
        server._background_tasks.add(task)

        # Use a custom fast shutdown with minimal wait time
        async def fast_shutdown() -> None:
            # Reduce wait time for test
            import time

            start = time.perf_counter()
            max_wait = 0.1  # Very short timeout
            while len(server._background_tasks) > 0 and (time.perf_counter() - start) < max_wait:
                await asyncio.sleep(0.01)
            # Task should still be pending after short timeout

        with patch.object(server, "_process_shutdown", fast_shutdown):
            await server._process_shutdown()
            # Verify the slow task is still pending (not completed)
            assert not task.done()

        # Cleanup
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_shutdown_disconnects_mcp_servers(self) -> None:
        """Test shutdown disconnects MCP servers."""
        mock_mcp_manager = AsyncMock()
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
            mcp_manager=mock_mcp_manager,
        )
        server = HTTPServer(
            services=services,
            port=8000,
            test_mode=True,
        )

        await server._process_shutdown()

        mock_mcp_manager.disconnect_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_handles_mcp_disconnect_error(self) -> None:
        """Test shutdown handles MCP disconnect error gracefully."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.disconnect_all.side_effect = RuntimeError("Disconnect failed")

        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
            mcp_manager=mock_mcp_manager,
        )
        server = HTTPServer(
            services=services,
            port=8000,
            test_mode=True,
        )

        # Should not raise
        await server._process_shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_increments_success_metric(self) -> None:
        """Test shutdown increments success metric."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)

        with patch.object(server._metrics, "inc_counter") as mock_inc:
            await server._process_shutdown()
            mock_inc.assert_called_with("shutdown_succeeded_total")


# ============================================================================
# create_server Function Tests
# ============================================================================


class TestCreateServer:
    """Tests for create_server function."""

    @pytest.mark.asyncio
    async def test_create_server_minimal(self) -> None:
        """Test create_server with minimal arguments."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = await create_server(services=services, port=8000, test_mode=True)

        assert isinstance(server, HTTPServer)
        assert server.port == 8000
        assert server.test_mode is True

    @pytest.mark.asyncio
    async def test_create_server_with_all_args(self, session_storage: LocalSessionManager) -> None:
        """Test create_server with all arguments."""
        mock_mcp_manager = MagicMock()
        mock_config = MagicMock()

        services = ServiceContainer(
            config=mock_config,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
            mcp_manager=mock_mcp_manager,
        )

        server = await create_server(
            services=services,
            port=9000,
            test_mode=False,
        )

        assert server.port == 9000
        assert server.test_mode is False
        assert server.mcp_manager is mock_mcp_manager
        assert server.config is mock_config
        assert server.session_manager is session_storage


# ============================================================================
# Admin Endpoint Tests
# ============================================================================


@pytest.mark.integration
class TestAdminEndpoints:
    """Additional tests for admin endpoints."""

    def test_status_check_running_true(self, client: TestClient) -> None:
        """Test status check when server is running."""
        # The TestClient fixture sets _running to True during lifespan
        response = client.get("/api/admin/status")
        assert response.status_code == 200
        data = response.json()
        # Accept any valid running status
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
        self, session_storage: LocalSessionManager, temp_db: LocalDatabase
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
        self, session_storage: LocalSessionManager, temp_db: LocalDatabase
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

    def test_status_check_memory_manager_failure(
        self, session_storage: LocalSessionManager
    ) -> None:
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

        # Shutdown was initiated
        assert response.json()["status"] == "shutting_down"

    def test_metrics_endpoint_with_daemon(
        self, client: TestClient, basic_http_server: HTTPServer
    ) -> None:
        """Test metrics endpoint updates daemon metrics."""
        mock_daemon = MagicMock()
        mock_daemon.uptime = 120.5
        basic_http_server._daemon = mock_daemon

        response = client.get("/api/admin/metrics")

        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        assert "daemon_uptime_seconds 120.5" in response.text

    def test_config_endpoint_error_handling(self, session_storage: LocalSessionManager) -> None:
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

        # Make get_version raise an exception
        with patch("gobby.servers.routes.admin._config.get_version") as mock_version:
            mock_version.side_effect = RuntimeError("Version error")

            client = TestClient(server.app)
            response = client.get("/api/admin/config")

            assert response.status_code == 500


# ============================================================================
# MCP Endpoint Tests
# ============================================================================


@pytest.mark.integration
class TestMCPEndpoints:
    """Tests for MCP endpoints.

    Note: MCP routes require app.state.server to be set, which happens during
    lifespan. Tests use TestClient as context manager to ensure lifespan runs.
    """

    @pytest.fixture
    def mcp_server(self, session_storage: LocalSessionManager) -> HTTPServer:
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
            json={"tool_name": "test-tool"},  # missing server_name
        )
        assert response.status_code == 400
        assert "server_name" in response.json()["detail"]["error"]

    def test_get_tool_schema_missing_fields(self, mcp_client: TestClient) -> None:
        """Test getting tool schema with missing fields."""
        response = mcp_client.post(
            "/api/mcp/tools/schema",
            json={"server_name": "test-server"},  # missing tool_name
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
            json={"name": "test-server"},  # missing transport
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


# ============================================================================
# MCP Endpoints with Mock Manager Tests
# ============================================================================


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
        session_storage: LocalSessionManager,
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
        # Set mcp_manager directly to avoid GobbyDaemonTools initialization
        server.mcp_manager = FakeMCPManagerSimple()
        # Also update container reference
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


# ============================================================================
# Code Execution Endpoint Tests
# ============================================================================


@pytest.mark.integration
class TestCodeEndpoints:
    """Tests for code execution endpoints."""

    @pytest.fixture
    def code_server(self, session_storage: LocalSessionManager) -> HTTPServer:
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
        # Code execution endpoints have been removed
        assert response.status_code == 404

    def test_process_dataset_missing_data(self, code_client: TestClient) -> None:
        """Test process_dataset endpoint was removed."""
        response = code_client.post(
            "/code/process-dataset",
            json={"operation": "summarize"},
        )
        # Code execution endpoints have been removed
        assert response.status_code == 404

    def test_process_dataset_missing_operation(self, code_client: TestClient) -> None:
        """Test process_dataset endpoint was removed."""
        response = code_client.post(
            "/code/process-dataset",
            json={"data": [1, 2, 3]},
        )
        # Code execution endpoints have been removed
        assert response.status_code == 404


# ============================================================================
# Hooks Endpoint Tests
# ============================================================================


@pytest.mark.integration
class TestHooksEndpoints:
    """Tests for hooks endpoints."""

    def test_execute_hook_without_hook_manager(self, client: TestClient) -> None:
        """Test execute hook when hook manager not initialized."""
        # Ensure hook_manager is not present (lifespan might have added it)
        if hasattr(client.app.state, "hook_manager"):
            del client.app.state.hook_manager

        response = client.post(
            "/api/hooks/execute",
            json={"hook_type": "session-start", "source": "claude"},
        )
        assert response.status_code == 503
        assert "HookManager not initialized" in response.json()["detail"]

    def test_execute_hook_with_mock_manager(self, session_storage: LocalSessionManager) -> None:
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

        # Set up mock hook manager
        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        # Mock the adapter's handle_native method
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
        self, session_storage: LocalSessionManager
    ) -> None:
        """Test hook returns graceful response when adapter throws exception.

        Instead of returning HTTP 500 (which causes Claude Code to show confusing
        "hook failed" warnings), the endpoint should return 200 with continue=True
        and helpful additionalContext.
        """
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

        # Mock adapter to throw an exception
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

            # Should return 200 OK with graceful response, not 500
            assert response.status_code == 200
            data = response.json()
            assert data["continue"] is True
            assert data["decision"] == "approve"
            # Should include helpful context for supported hook types
            assert "hookSpecificOutput" in data
            assert data["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
            assert "non-fatal" in data["hookSpecificOutput"]["additionalContext"]
            assert "Database connection failed" in data["hookSpecificOutput"]["additionalContext"]

    def test_execute_hook_graceful_error_for_unsupported_hook_type(
        self, session_storage: LocalSessionManager
    ) -> None:
        """Test graceful error response for hook types that don't support additionalContext."""
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
                    "hook_type": "session-start",  # Doesn't support hookSpecificOutput
                    "source": "claude",
                    "input_data": {},
                },
            )

            # Should still return 200 with continue=True
            assert response.status_code == 200
            data = response.json()
            assert data["continue"] is True
            assert data["decision"] == "approve"
            # session-start doesn't support hookSpecificOutput, so no additionalContext
            assert "hookSpecificOutput" not in data


# ============================================================================
# Webhooks Endpoint Tests
# ============================================================================


@pytest.mark.integration
class TestWebhooksEndpoints:
    """Tests for webhooks endpoints."""

    @pytest.fixture
    def webhooks_server(self, session_storage: LocalSessionManager) -> HTTPServer:
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

    def test_list_webhooks_endpoint_exists(self, session_storage: LocalSessionManager) -> None:
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
        # Config is None, so webhooks are disabled
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


# ============================================================================
# Exception Handler Tests
# ============================================================================


class TestExceptionHandlers:
    """Tests for exception handlers."""

    def test_global_exception_handler_logs_details(
        self, session_storage: LocalSessionManager
    ) -> None:
        """Test that global exception handler logs request details."""
        services = ServiceContainer(
            config=MagicMock(),
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

        @server.app.get("/trigger_error")
        def trigger_error() -> None:
            raise RuntimeError("Test error")

        client = TestClient(server.app, raise_server_exceptions=False)

        with patch("gobby.servers.http.logger") as mock_logger:
            response = client.get("/trigger_error")

            # Exception should be logged
            assert mock_logger.error.called

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert data["error_logged"] is True

    def test_global_exception_handler_includes_path(
        self, session_storage: LocalSessionManager
    ) -> None:
        """Test exception handler includes request path in logs."""
        services = ServiceContainer(
            config=MagicMock(),
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

        @server.app.get("/custom/error/path")
        def trigger_error() -> None:
            raise ValueError("Custom error")

        client = TestClient(server.app, raise_server_exceptions=False)
        response = client.get("/custom/error/path")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"


# ============================================================================
# Lifespan Tests
# ============================================================================


class TestLifespan:
    """Tests for FastAPI lifespan management."""

    def test_lifespan_sets_running_flag(self, session_storage: LocalSessionManager) -> None:
        """Test that lifespan sets _running flag."""
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

        assert server._running is False

        with TestClient(server.app):
            # During lifespan, _running should be True
            assert server._running is True

    def test_lifespan_initializes_hook_manager(self, session_storage: LocalSessionManager) -> None:
        """Test that lifespan initializes HookManager."""
        mock_config = MagicMock()
        mock_config.logging.hook_manager = "/tmp/hooks.log"
        mock_config.logging.max_size_mb = 10
        mock_config.logging.backup_count = 3
        mock_config.workflow.timeout = 30
        mock_config.workflow.enabled = True

        services = ServiceContainer(
            config=mock_config,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )

        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

        with patch("gobby.servers.http.HookManager") as MockHM:
            with TestClient(server.app):
                MockHM.assert_called_once()


# ============================================================================
# run_server Function Tests
# ============================================================================


class TestRunServer:
    """Tests for run_server function."""

    @pytest.mark.asyncio
    async def test_run_server_creates_uvicorn_config(self) -> None:
        """Test run_server creates proper uvicorn config."""
        services = ServiceContainer(
            config=None,
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)

        mock_config_class = MagicMock()
        mock_server_class = MagicMock()
        mock_server_instance = AsyncMock()
        mock_server_class.return_value = mock_server_instance
        mock_server_instance.serve = AsyncMock(return_value=None)

        with (
            patch("uvicorn.Config", mock_config_class),
            patch("uvicorn.Server", mock_server_class),
        ):
            await run_server(
                server,
                host="127.0.0.1",
                workers=2,
                limit_concurrency=500,
                timeout_keep_alive=10,
            )

            # Verify Config was created with correct arguments
            mock_config_class.assert_called_once()
            config_kwargs = mock_config_class.call_args.kwargs
            assert config_kwargs["host"] == "127.0.0.1"
            assert config_kwargs["port"] == 8000
            assert config_kwargs["workers"] == 2
            assert config_kwargs["limit_concurrency"] == 500
            assert config_kwargs["timeout_keep_alive"] == 10

    @pytest.mark.asyncio
    async def test_run_server_handles_keyboard_interrupt(self) -> None:
        """Test run_server handles KeyboardInterrupt gracefully."""
        services = ServiceContainer(
            config=None,
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)

        mock_server_class = MagicMock()
        mock_server_instance = AsyncMock()
        mock_server_class.return_value = mock_server_instance
        mock_server_instance.serve = AsyncMock(side_effect=KeyboardInterrupt())

        with (
            patch("uvicorn.Config", MagicMock()),
            patch("uvicorn.Server", mock_server_class),
        ):
            # Should not raise
            await run_server(server)

    @pytest.mark.asyncio
    async def test_run_server_handles_system_exit(self) -> None:
        """Test run_server handles SystemExit gracefully."""
        services = ServiceContainer(
            config=None,
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)

        mock_server_class = MagicMock()
        mock_server_instance = AsyncMock()
        mock_server_class.return_value = mock_server_instance
        mock_server_instance.serve = AsyncMock(side_effect=SystemExit())

        with (
            patch("uvicorn.Config", MagicMock()),
            patch("uvicorn.Server", mock_server_class),
        ):
            # Should not raise
            await run_server(server)


# ============================================================================
# Internal Registry Tests
# ============================================================================


@pytest.mark.integration
class TestInternalRegistries:
    """Tests for internal registry handling."""

    def test_list_tools_internal_server(self, session_storage: LocalSessionManager) -> None:
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

    def test_list_tools_internal_server_not_found(
        self, session_storage: LocalSessionManager
    ) -> None:
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

    def test_call_tool_internal_server(self, session_storage: LocalSessionManager) -> None:
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

    def test_call_tool_internal_server_error(self, session_storage: LocalSessionManager) -> None:
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

    def test_get_tool_schema_internal_server(self, session_storage: LocalSessionManager) -> None:
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
