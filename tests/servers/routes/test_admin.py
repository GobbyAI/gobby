import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.servers.routes.admin import create_admin_router
from gobby.shutdown_intent import ShutdownIntent

pytestmark = pytest.mark.unit


class RunnerShutdownStub:
    def __init__(self) -> None:
        self._shutdown_requested = False
        self._shutdown_intent: ShutdownIntent | None = None
        self.request_shutdown_calls: list[ShutdownIntent | None] = []

    def request_shutdown(self, intent: ShutdownIntent | None = None) -> None:
        self.request_shutdown_calls.append(intent)
        if intent is not None:
            self._shutdown_intent = intent
        self._shutdown_requested = True


class MinimalRunnerFallbackStub:
    def __init__(self) -> None:
        self._shutdown_requested = False
        self._shutdown_intent: ShutdownIntent | None = None


class TestAdminRoutes:
    @pytest.fixture(autouse=True)
    def reset_restart_state(self):
        import gobby.servers.routes.admin._lifecycle as lifecycle

        lifecycle._restart_lock = None
        yield
        lifecycle._restart_lock = None

    @pytest.fixture
    def mock_server(self):
        server = MagicMock()
        server._start_time = 1234567890.0
        server._running = True
        server.port = 60887
        server.test_mode = False

        # Mock Daemon
        server._daemon = MagicMock()
        server._daemon.status.return_value = {"status": "running"}
        server._daemon.uptime = 100.0

        # Mock Managers
        server.mcp_manager = MagicMock()
        server.mcp_manager.server_configs = []
        server.mcp_manager.health = {}
        server.mcp_manager.connections = {}

        server._internal_manager = MagicMock()
        server._internal_manager.get_all_registries.return_value = []

        server.session_manager = MagicMock()
        server.session_manager.count_by_status.return_value = {"active": 1, "paused": 0}

        server.task_manager = MagicMock()
        server.task_manager.count_by_state.return_value = {"ready": 2}
        server.task_manager.count_ready_tasks.return_value = 1
        server.task_manager.count_blocked_tasks.return_value = 0

        server.memory_manager = MagicMock()
        server.memory_manager.get_stats.return_value = {"total_count": 10}
        server.memory_manager._vector_store = None
        server.memory_manager._neo4j_client = None

        server._background_tasks = set()
        server._runner = RunnerShutdownStub()

        # Shutdown support
        server._process_shutdown = AsyncMock()

        return server

    @pytest.fixture
    def client(self, mock_server):
        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        return TestClient(app)

    @patch("gobby.servers.routes.admin._health.psutil")
    @patch("gobby.servers.routes.admin._health.asyncio.to_thread")
    def test_status_endpoint(self, mock_to_thread, mock_psutil, client, mock_server) -> None:
        # Mock psutil
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(
            rss=1024 * 1024 * 100, vms=1024 * 1024 * 200
        )
        mock_process.num_threads.return_value = 10
        mock_psutil.Process.return_value = mock_process

        # Mock asyncio.to_thread for cpu_percent (awaitable)
        mock_to_thread.return_value = 1.5

        # Mock MCP servers config
        mock_config = MagicMock()
        mock_config.name = "test-server"
        mock_config.enabled = True
        mock_config.transport = "stdio"
        mock_server.mcp_manager.server_configs = [mock_config]
        mock_server.mcp_manager.connections = ["test-server"]

        response = client.get("/api/admin/status")
        assert response.status_code == 200
        data = response.json()

        assert data["daemon"]["status"] == "running"
        assert data["process"]["cpu_percent"] == 1.5
        assert data["process"]["memory_rss_mb"] == 100.0
        assert "test-server" in data["mcp_servers"]
        assert data["mcp_servers"]["test-server"]["connected"] is True

    @patch("gobby.servers.routes.admin._health.is_qdrant_healthy", new_callable=AsyncMock)
    @patch("gobby.servers.routes.admin._health.psutil")
    @patch("gobby.servers.routes.admin._health.asyncio.to_thread")
    def test_status_endpoint_uses_qdrant_service_health_when_url_configured(
        self,
        mock_to_thread,
        mock_psutil,
        mock_is_qdrant_healthy,
        client,
        mock_server,
    ) -> None:
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(
            rss=1024 * 1024 * 100, vms=1024 * 1024 * 200
        )
        mock_process.num_threads.return_value = 10
        mock_psutil.Process.return_value = mock_process
        mock_to_thread.return_value = 0.5
        mock_is_qdrant_healthy.return_value = True

        vector_store = MagicMock()
        vector_store._client = None
        vector_store._url = "http://localhost:6333"
        mock_server.memory_manager._vector_store = vector_store

        response = client.get("/api/admin/status")
        assert response.status_code == 200

        data = response.json()
        assert data["memory"]["qdrant"] == {"configured": True, "healthy": True}
        mock_is_qdrant_healthy.assert_awaited_once_with("http://localhost:6333")

    @patch("gobby.servers.routes.admin._health.get_all_metrics")
    @patch("gobby.servers.routes.admin._health.generate_latest")
    @patch("gobby.servers.routes.admin._health.psutil")
    def test_metrics_endpoint(self, mock_psutil, mock_generate, mock_get_all, client) -> None:
        mock_generate.return_value = b"metric_name 1.0\n"
        mock_get_all.return_value = {"counters": {}, "gauges": {}, "histograms": {}}

        # Mock psutil for daemon metrics
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(rss=1000)
        mock_process.cpu_percent.return_value = 0.5
        mock_psutil.Process.return_value = mock_process

        response = client.get("/api/admin/metrics")
        assert response.status_code == 200
        assert response.text == "metric_name 1.0\n"
        assert "text/plain" in response.headers["content-type"]

    @patch("gobby.servers.routes.admin._config.get_version")
    def test_config_endpoint(self, mock_get_version, client) -> None:
        mock_get_version.return_value = "1.0.0"

        response = client.get("/api/admin/config")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["config"]["server"]["version"] == "1.0.0"
        assert data["config"]["features"]["session_manager"] is True

    def test_shutdown_endpoint(self, client, mock_server) -> None:
        # We don't need to patch shutdown_event, admin.py calls server._process_shutdown()

        with patch("gobby.runner_maintenance.write_shutdown_source") as mock_write_shutdown:
            response = client.post("/api/admin/shutdown")
        assert response.status_code == 200
        assert response.json() == {
            "status": "shutting_down",
            "message": "Graceful shutdown initiated",
            "response_time_ms": response.json()["response_time_ms"],  # ignore value
        }
        mock_write_shutdown.assert_called_once_with("http_shutdown", intent="stop")
        assert mock_server._runner._shutdown_requested is True
        assert mock_server._runner._shutdown_intent is ShutdownIntent.STOP
        assert mock_server._runner.request_shutdown_calls == [ShutdownIntent.STOP]

        # Verify shutdown was initiated
        # Note: TestClient runs synchronous, but create_task might loop issues.
        # But endpoints call asyncio.create_task.
        # Since we use TestClient, it might not actually run the task loop unless we handle it,
        # but the endpoint function itself executed up to return.

        # Verify shutdown was initiated
        # Instead of checking background_tasks (which might clear quickly via callback),
        # verify the method was called.
        mock_server._process_shutdown.assert_called()

    def test_request_runner_shutdown_falls_back_to_runner_attrs(self) -> None:
        from gobby.servers.routes.admin._lifecycle import _request_runner_shutdown

        runner = MinimalRunnerFallbackStub()
        _request_runner_shutdown(SimpleNamespace(_runner=runner), ShutdownIntent.RESTART)

        assert runner._shutdown_requested is True
        assert runner._shutdown_intent is ShutdownIntent.RESTART

    @patch("gobby.servers.routes.admin._lifecycle.os.getpid", return_value=4321)
    @patch(
        "gobby.servers.routes.admin._lifecycle._should_restart_via_service_manager",
        return_value=False,
    )
    @patch("gobby.servers.routes.admin._lifecycle.subprocess.Popen")
    def test_restart_endpoint_uses_direct_helper(
        self,
        mock_popen,
        _mock_service_mode,
        _mock_getpid,
        client,
        mock_server,
    ) -> None:
        import gobby.servers.routes.admin._lifecycle as lifecycle

        with patch("gobby.runner_maintenance.write_shutdown_source") as mock_write_shutdown:
            response = client.post("/api/admin/restart")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "restarting"
        assert data["message"] == "Daemon restart initiated"
        assert "response_time_ms" in data

        mock_popen.assert_called_once()
        command = mock_popen.call_args.args[0]
        assert command == [sys.executable, "-c", lifecycle._DIRECT_RESTART_HELPER, "4321"]
        mock_write_shutdown.assert_called_once_with("http_restart", intent="restart")
        assert mock_server._runner._shutdown_requested is True
        assert mock_server._runner._shutdown_intent is ShutdownIntent.RESTART
        assert mock_server._runner.request_shutdown_calls == [ShutdownIntent.RESTART]

        mock_server._process_shutdown.assert_called()

    @patch("gobby.servers.routes.admin._lifecycle.os.getpid", return_value=4321)
    @patch(
        "gobby.servers.routes.admin._lifecycle._should_restart_via_service_manager",
        return_value=True,
    )
    @patch("gobby.servers.routes.admin._lifecycle.subprocess.Popen")
    def test_restart_endpoint_uses_service_helper(
        self,
        mock_popen,
        _mock_service_mode,
        _mock_getpid,
        client,
        mock_server,
    ) -> None:
        import gobby.servers.routes.admin._lifecycle as lifecycle

        with patch("gobby.runner_maintenance.write_shutdown_source") as mock_write_shutdown:
            response = client.post("/api/admin/restart")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "restarting"
        assert data["message"] == "Daemon restart initiated"
        assert "response_time_ms" in data

        mock_popen.assert_called_once()
        command = mock_popen.call_args.args[0]
        assert command == [
            sys.executable,
            "-c",
            lifecycle._SERVICE_RESTART_HELPER,
            "4321",
            str(mock_server.port),
            "http_restart",
        ]
        mock_write_shutdown.assert_called_once_with("http_restart", intent="restart")
        assert mock_server._runner._shutdown_requested is True
        assert mock_server._runner._shutdown_intent is ShutdownIntent.RESTART
        assert mock_server._runner.request_shutdown_calls == [ShutdownIntent.RESTART]

        mock_server._process_shutdown.assert_called()

    @patch(
        "gobby.servers.routes.admin._lifecycle._should_restart_via_service_manager",
        return_value=False,
    )
    @patch("gobby.servers.routes.admin._lifecycle.subprocess.Popen")
    def test_restart_endpoint_double_restart_guard(
        self,
        mock_popen,
        _mock_service_mode,
        client,
        mock_server,
    ) -> None:
        # First restart should succeed
        with patch("gobby.runner_maintenance.write_shutdown_source") as mock_write_shutdown:
            response1 = client.post("/api/admin/restart")
            response2 = client.post("/api/admin/restart")
        assert response1.json()["status"] == "restarting"

        # Second restart should be rejected
        assert response2.json()["status"] == "already_restarting"
        mock_write_shutdown.assert_called_once_with("http_restart", intent="restart")
        mock_popen.assert_called_once()


class TestAdminRestartHelpers:
    @patch("gobby.servers.routes.admin._lifecycle._append_restart_helper_log")
    @patch("gobby.cli.installers.service.service_restart")
    @patch("gobby.cli.daemon._wait_for_daemon_health")
    @patch("gobby.servers.routes.admin._lifecycle._wait_for_process_exit", return_value=True)
    def test_service_restart_helper_invokes_service_restart_when_needed(
        self,
        _mock_wait_for_exit,
        mock_wait_for_health,
        mock_service_restart,
        mock_log,
    ) -> None:
        import gobby.servers.routes.admin._lifecycle as lifecycle

        mock_wait_for_health.side_effect = [None, 0.5]
        mock_service_restart.return_value = {"success": True, "method": "launchctl"}

        lifecycle._run_service_restart_helper(4321, 60887, "http_restart")

        mock_service_restart.assert_called_once_with(shutdown_source="http_restart")
        assert mock_service_restart.call_count == 1
        assert mock_service_restart.call_args is not None
        assert mock_wait_for_health.call_count == 2
        mock_log.assert_not_called()
        assert mock_log.call_count == 0
        assert not mock_log.called


class TestHealthEndpoint:
    """Tests for GET /admin/health."""

    @pytest.fixture
    def mock_server(self):
        server = MagicMock()
        server.test_mode = False
        return server

    @pytest.fixture
    def client(self, mock_server):
        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        return TestClient(app)

    def test_health_returns_ok(self, client) -> None:
        response = client.get("/api/admin/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_health_is_lightweight(self, client) -> None:
        """Health check should return quickly with no I/O."""
        response = client.get("/api/admin/health")
        assert response.status_code == 200
        data = response.json()
        # Should only have a single key
        assert list(data.keys()) == ["status"]


class TestWorkflowsReloadEndpoint:
    """Tests for POST /admin/workflows/reload."""

    @pytest.fixture
    def mock_server(self):
        server = MagicMock()
        server.test_mode = False
        server._background_tasks = set()

        # Internal manager with workflows registry
        workflows_registry = MagicMock()
        workflows_registry.name = "gobby-workflows"
        workflows_registry.call = AsyncMock(return_value={"reloaded": 5})

        server._internal_manager = MagicMock()
        server._internal_manager.get_all_registries.return_value = [workflows_registry]

        return server

    @pytest.fixture
    def client(self, mock_server):
        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        return TestClient(app)

    def test_reload_workflows_success(self, client) -> None:
        response = client.post("/api/admin/workflows/reload")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["message"] == "Workflow cache reloaded"
        assert data["details"] == {"reloaded": 5}
        assert "response_time_ms" in data

    def test_reload_workflows_no_registry(self, client, mock_server) -> None:
        # Return registries that don't include gobby-workflows
        other_registry = MagicMock()
        other_registry.name = "gobby-tasks"
        mock_server._internal_manager.get_all_registries.return_value = [other_registry]

        response = client.post("/api/admin/workflows/reload")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "error"
        assert data["message"] == "Workflow registry not available"

    def test_reload_workflows_no_internal_manager(self, client, mock_server) -> None:
        mock_server._internal_manager = None

        response = client.post("/api/admin/workflows/reload")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "error"
        assert data["message"] == "Workflow registry not available"

    def test_reload_workflows_tool_not_found(self, client, mock_server) -> None:
        registry = mock_server._internal_manager.get_all_registries.return_value[0]
        registry.call = AsyncMock(side_effect=ValueError("Tool not found"))

        response = client.post("/api/admin/workflows/reload")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "error"
        assert data["message"] == "reload_cache tool not found"

    def test_reload_workflows_call_exception(self, client, mock_server) -> None:
        registry = mock_server._internal_manager.get_all_registries.return_value[0]
        registry.call = AsyncMock(side_effect=RuntimeError("Cache corrupted"))

        response = client.post("/api/admin/workflows/reload")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "error"
        assert "Failed to reload cache" in data["message"]


class TestTestEndpoints:
    """Tests for /admin/test/* endpoints (E2E test-mode only)."""

    @pytest.fixture
    def mock_server(self):
        server = MagicMock()
        server.test_mode = True
        server._background_tasks = set()

        # Session manager with db
        server.session_manager = MagicMock()
        server.session_manager.db = MagicMock()
        server.session_manager.update_usage.return_value = True

        return server

    @pytest.fixture
    def client(self, mock_server):
        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        return TestClient(app)

    # --- register-project ---

    @patch("gobby.storage.projects.LocalProjectManager")
    def test_register_project_success(self, mock_pm_cls, client, mock_server) -> None:
        mock_pm = MagicMock()
        mock_pm.get.return_value = None  # project does not exist yet
        mock_pm_cls.return_value = mock_pm

        response = client.post(
            "/api/admin/test/register-project",
            json={"project_id": "proj-1", "name": "Test Project"},
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["project_id"] == "proj-1"
        assert data["name"] == "Test Project"
        assert "response_time_ms" in data

    @patch("gobby.storage.projects.LocalProjectManager")
    def test_register_project_already_exists(self, mock_pm_cls, client) -> None:
        existing = MagicMock()
        existing.id = "proj-1"
        existing.name = "Existing"

        mock_pm = MagicMock()
        mock_pm.get.return_value = existing
        mock_pm_cls.return_value = mock_pm

        response = client.post(
            "/api/admin/test/register-project",
            json={"project_id": "proj-1", "name": "Test"},
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "already_exists"
        assert data["project_id"] == "proj-1"

    def test_register_project_forbidden_when_not_test_mode(self, mock_server) -> None:
        mock_server.test_mode = False

        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        client = TestClient(app)

        response = client.post(
            "/api/admin/test/register-project",
            json={"project_id": "proj-1", "name": "Test"},
        )
        assert response.status_code == 403
        assert "test mode" in response.json()["detail"].lower()

    @patch("gobby.storage.projects.LocalProjectManager")
    def test_register_project_no_session_manager(self, mock_pm_cls, client, mock_server) -> None:
        mock_server.session_manager = None

        response = client.post(
            "/api/admin/test/register-project",
            json={"project_id": "proj-1", "name": "Test"},
        )
        # HTTPException(503) caught by generic except → re-raised as 500
        assert response.status_code == 500

    # --- register-agent ---

    @patch("gobby.storage.agents.LocalAgentRunManager")
    def test_register_agent_success(self, mock_arm_cls, client) -> None:
        mock_arm = MagicMock()
        mock_run = MagicMock()
        mock_run.to_dict.return_value = {
            "run_id": "run-1",
            "id": "run-1",
            "session_id": "sess-1",
            "parent_session_id": "parent-1",
            "mode": "interactive",
        }
        mock_arm.get.return_value = mock_run
        mock_arm_cls.return_value = mock_arm

        response = client.post(
            "/api/admin/test/register-agent",
            json={
                "run_id": "run-1",
                "session_id": "sess-1",
                "parent_session_id": "parent-1",
                "mode": "interactive",
            },
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["agent"]["run_id"] == "run-1"
        mock_arm.create.assert_called_once()
        mock_arm.start.assert_called_once_with("run-1")

    def test_register_agent_forbidden_when_not_test_mode(self, mock_server) -> None:
        mock_server.test_mode = False

        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        client = TestClient(app)

        response = client.post(
            "/api/admin/test/register-agent",
            json={
                "run_id": "run-1",
                "session_id": "sess-1",
                "parent_session_id": "parent-1",
            },
        )
        assert response.status_code == 403

    # --- unregister-agent ---

    @patch("gobby.storage.agents.LocalAgentRunManager")
    def test_unregister_agent_success(self, mock_arm_cls, client) -> None:
        mock_arm = MagicMock()
        mock_arm.get.return_value = MagicMock()  # agent found
        mock_arm_cls.return_value = mock_arm

        response = client.delete("/api/admin/test/unregister-agent/run-1")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert "run-1" in data["message"]
        mock_arm.fail.assert_called_once_with("run-1", error="Unregistered via test endpoint")

    @patch("gobby.storage.agents.LocalAgentRunManager")
    def test_unregister_agent_not_found(self, mock_arm_cls, client) -> None:
        mock_arm = MagicMock()
        mock_arm.get.return_value = None  # agent not found
        mock_arm_cls.return_value = mock_arm

        response = client.delete("/api/admin/test/unregister-agent/run-nonexistent")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "not_found"

    def test_unregister_agent_forbidden_when_not_test_mode(self, mock_server) -> None:
        mock_server.test_mode = False

        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        client = TestClient(app)

        response = client.delete("/api/admin/test/unregister-agent/run-1")
        assert response.status_code == 403

    # --- set-session-usage ---

    def test_set_session_usage_success(self, client, mock_server) -> None:
        mock_server.session_manager.update_usage.return_value = True

        response = client.post(
            "/api/admin/test/set-session-usage",
            json={
                "session_id": "sess-1",
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_creation_tokens": 200,
                "cache_read_tokens": 100,
            },
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["session_id"] == "sess-1"
        assert data["usage_set"]["input_tokens"] == 1000
        assert data["usage_set"]["output_tokens"] == 500

        mock_server.session_manager.update_usage.assert_called_once_with(
            session_id="sess-1",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=200,
            cache_read_tokens=100,
        )

    def test_set_session_usage_not_found(self, client, mock_server) -> None:
        mock_server.session_manager.update_usage.return_value = False

        response = client.post(
            "/api/admin/test/set-session-usage",
            json={"session_id": "nonexistent"},
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "not_found"
        assert "nonexistent" in data["message"]

    def test_set_session_usage_defaults(self, client, mock_server) -> None:
        """When only session_id is provided, defaults should be zero."""
        mock_server.session_manager.update_usage.return_value = True

        response = client.post(
            "/api/admin/test/set-session-usage",
            json={"session_id": "sess-1"},
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["usage_set"]["input_tokens"] == 0
        assert data["usage_set"]["output_tokens"] == 0

    def test_set_session_usage_forbidden_when_not_test_mode(self, mock_server) -> None:
        mock_server.test_mode = False

        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        client = TestClient(app)

        response = client.post(
            "/api/admin/test/set-session-usage",
            json={"session_id": "sess-1"},
        )
        assert response.status_code == 403

    def test_set_session_usage_no_session_manager(self, client, mock_server) -> None:
        mock_server.session_manager = None

        response = client.post(
            "/api/admin/test/set-session-usage",
            json={"session_id": "sess-1"},
        )
        # HTTPException(503) caught by generic except → re-raised as 500
        assert response.status_code == 500


class TestSetupStateEndpoints:
    """Tests for GET/POST /admin/setup-state using real temp files."""

    @pytest.fixture
    def mock_server(self):
        server = MagicMock()
        server.test_mode = False
        return server

    @pytest.fixture
    def client(self, mock_server):
        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        return TestClient(app)

    @pytest.fixture
    def setup_home(self, tmp_path, monkeypatch):
        """Redirect HOME so expanduser() resolves to tmp_path."""
        monkeypatch.setenv("HOME", str(tmp_path))
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        return gobby_dir / "setup_state.json"

    # --- GET /admin/setup-state ---

    def test_get_setup_state_file_exists(self, client, setup_home) -> None:
        import json

        setup_home.write_text(json.dumps({"step": "complete", "provider": "anthropic"}))

        response = client.get("/api/admin/setup-state")
        assert response.status_code == 200
        data = response.json()

        assert data["exists"] is True
        assert data["step"] == "complete"
        assert data["provider"] == "anthropic"

    def test_get_setup_state_no_file(self, client, setup_home) -> None:
        # Don't create the file
        response = client.get("/api/admin/setup-state")
        assert response.status_code == 200
        data = response.json()

        assert data["exists"] is False

    def test_get_setup_state_invalid_json(self, client, setup_home) -> None:
        setup_home.write_text("not valid json {")

        response = client.get("/api/admin/setup-state")
        assert response.status_code == 200
        data = response.json()

        assert data["exists"] is False
        assert "error" in data

    # --- POST /admin/setup-state ---

    def test_update_setup_state_success(self, client, setup_home) -> None:
        import json

        setup_home.write_text(json.dumps({"step": "provider"}))

        response = client.post(
            "/api/admin/setup-state",
            json={"web_onboarding_complete": True},
        )
        assert response.status_code == 200
        data = response.json()

        assert data["success"] is True
        written = json.loads(setup_home.read_text())
        assert written["web_onboarding_complete"] is True
        assert written["step"] == "provider"

    def test_update_setup_state_no_file(self, client, setup_home) -> None:
        response = client.post(
            "/api/admin/setup-state",
            json={"web_onboarding_complete": True},
        )
        assert response.status_code == 200
        data = response.json()

        assert data["success"] is False
        assert "No setup state found" in data["error"]

    def test_update_setup_state_invalid_json(self, client, setup_home) -> None:
        setup_home.write_text("bad json")

        response = client.post(
            "/api/admin/setup-state",
            json={"web_onboarding_complete": True},
        )
        assert response.status_code == 200
        data = response.json()

        assert data["success"] is False
        assert "error" in data

    def test_update_setup_state_false_flag_no_mutation(self, client, setup_home) -> None:
        """When web_onboarding_complete=False, the key should not be set."""
        import json

        setup_home.write_text(json.dumps({"step": "provider"}))

        response = client.post(
            "/api/admin/setup-state",
            json={"web_onboarding_complete": False},
        )
        assert response.status_code == 200
        data = response.json()

        assert data["success"] is True
        written = json.loads(setup_home.read_text())
        assert "web_onboarding_complete" not in written
