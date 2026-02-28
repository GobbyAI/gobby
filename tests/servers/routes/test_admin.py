from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.servers.routes.admin import create_admin_router

pytestmark = pytest.mark.unit


class TestAdminRoutes:
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
        server.task_manager.count_by_status.return_value = {"open": 2}
        server.task_manager.count_ready_tasks.return_value = 1
        server.task_manager.count_blocked_tasks.return_value = 0

        server.memory_manager = MagicMock()
        server.memory_manager.get_stats.return_value = {"total_count": 10}

        server._background_tasks = set()

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

    @patch("gobby.servers.routes.admin.psutil")
    @patch("gobby.servers.routes.admin.asyncio.to_thread")
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

    @patch("gobby.servers.routes.admin.get_metrics_collector")
    @patch("gobby.servers.routes.admin.psutil")
    def test_metrics_endpoint(self, mock_psutil, mock_get_collector, client) -> None:
        mock_collector = MagicMock()
        mock_collector.export_prometheus.return_value = "metric_name 1.0\n"
        mock_get_collector.return_value = mock_collector

        # Mock psutil for daemon metrics
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(rss=1000)
        mock_process.cpu_percent.return_value = 0.5
        mock_psutil.Process.return_value = mock_process

        response = client.get("/api/admin/metrics")
        assert response.status_code == 200
        assert response.text == "metric_name 1.0\n"
        assert "text/plain" in response.headers["content-type"]

    @patch("gobby.servers.routes.admin.get_version")
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

        response = client.post("/api/admin/shutdown")
        assert response.status_code == 200
        assert response.json() == {
            "status": "shutting_down",
            "message": "Graceful shutdown initiated",
            "response_time_ms": response.json()["response_time_ms"],  # ignore value
        }

        # Verify shutdown was initiated
        # Note: TestClient runs synchronous, but create_task might loop issues.
        # But endpoints call asyncio.create_task.
        # Since we use TestClient, it might not actually run the task loop unless we handle it,
        # but the endpoint function itself executed up to return.

        # Verify shutdown was initiated
        # Instead of checking background_tasks (which might clear quickly via callback),
        # verify the method was called.
        mock_server._process_shutdown.assert_called()

    @patch("gobby.servers.routes.admin.subprocess.Popen")
    def test_restart_endpoint(self, mock_popen, client, mock_server) -> None:
        response = client.post("/api/admin/restart")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "restarting"
        assert data["message"] == "Daemon restart initiated"
        assert "response_time_ms" in data

        # Verify restarter subprocess was spawned
        mock_popen.assert_called_once()

        # Verify shutdown was initiated
        mock_server._process_shutdown.assert_called()

    @patch("gobby.servers.routes.admin.subprocess.Popen")
    def test_restart_endpoint_double_restart_guard(self, mock_popen, client, mock_server) -> None:
        # First restart should succeed
        response1 = client.post("/api/admin/restart")
        assert response1.json()["status"] == "restarting"

        # Second restart should be rejected
        response2 = client.post("/api/admin/restart")
        assert response2.json()["status"] == "already_restarting"


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


class TestModelsEndpoint:
    """Tests for GET /admin/models."""

    @pytest.fixture
    def mock_server(self):
        server = MagicMock()
        server.test_mode = False
        # Config with default model
        server.services.config.llm_providers.default_model = "haiku"
        return server

    @pytest.fixture
    def client(self, mock_server):
        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        return TestClient(app)

    @patch("gobby.servers.routes.admin._discover_models")
    def test_models_returns_grouped(self, mock_discover, client) -> None:
        mock_discover.return_value = {
            "claude": ["haiku"],
            "gpt": ["gpt-4.5-preview", "o3-mini"],
        }

        response = client.get("/api/admin/models")
        assert response.status_code == 200
        data = response.json()

        assert "models" in data
        assert "claude" in data["models"]
        assert "gpt" in data["models"]
        assert data["default_model"] == "haiku"

    @patch("gobby.servers.routes.admin._discover_models")
    def test_models_provider_filter(self, mock_discover, client) -> None:
        mock_discover.return_value = {
            "claude": ["haiku"],
            "gpt": ["gpt-4.5-preview"],
        }

        response = client.get("/api/admin/models?provider=claude")
        assert response.status_code == 200
        data = response.json()

        assert "claude" in data["models"]
        assert "gpt" not in data["models"]

    @patch("gobby.servers.routes.admin._discover_models")
    def test_models_provider_filter_no_match(self, mock_discover, client) -> None:
        mock_discover.return_value = {
            "claude": ["haiku"],
        }

        response = client.get("/api/admin/models?provider=nonexistent")
        assert response.status_code == 200
        data = response.json()
        assert data["models"] == {}

    @patch("gobby.servers.routes.admin._fallback_models_from_config")
    @patch("gobby.servers.routes.admin._discover_models")
    def test_models_fallback_on_litellm_error(
        self, mock_discover, mock_fallback, client, mock_server
    ) -> None:
        mock_discover.side_effect = ImportError("No module named 'litellm'")
        mock_fallback.return_value = {"claude": ["haiku"]}

        response = client.get("/api/admin/models")
        assert response.status_code == 200
        data = response.json()

        assert data["models"] == {"claude": ["haiku"]}
        mock_fallback.assert_called_once_with(mock_server)

    @patch("gobby.servers.routes.admin._discover_models")
    def test_models_default_model_from_config(self, mock_discover, client) -> None:
        mock_discover.return_value = {}

        response = client.get("/api/admin/models")
        data = response.json()
        assert data["default_model"] == "haiku"

    @patch("gobby.servers.routes.admin._discover_models")
    def test_models_default_model_fallback(self, mock_discover) -> None:
        """When no config default_model is set, falls back to 'opus'."""
        server = MagicMock()
        server.test_mode = False
        server.services.config.llm_providers.default_model = None

        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(server)
        app.include_router(router)
        client = TestClient(app)

        mock_discover.return_value = {}
        response = client.get("/api/admin/models")
        data = response.json()
        assert data["default_model"] == "opus"


class TestDiscoverModels:
    """Tests for the _discover_models helper function."""

    @patch(
        "litellm.model_cost",
        {
            "haiku": {},
            "gpt-4.5-preview": {},
            "o3-mini": {},
            "gemini-2-flash": {},
        },
    )
    def test_discover_models_groups_by_provider(self) -> None:
        from gobby.servers.routes.admin import _discover_models

        result = _discover_models()

        assert "claude" in result
        assert "codex" in result
        assert "gemini" in result
        assert result["claude"] == [
            {"value": "", "label": "(default)"},
            {"value": "haiku", "label": "Haiku"}
        ]
        assert result["codex"] == [
            {"value": "", "label": "(default)"},
            {"value": "gpt-4.5-preview", "label": "GPT 4.5 Preview"},
            {"value": "o3-mini", "label": "O3 Mini"}
        ]

    @patch(
        "litellm.model_cost",
        {
            "haiku": {},
            "haiku-20250514": {},
            "gpt-4.5-preview": {},
            "gpt-4.5-preview-20250227": {},
        },
    )
    def test_discover_models_excludes_dated_variants(self) -> None:
        from gobby.servers.routes.admin import _discover_models

        result = _discover_models()

        assert any(m["value"] == "haiku" for m in result["claude"])
        assert not any(m["value"] == "haiku-20250514" for m in result["claude"])
        assert any(m["value"] == "gpt-4.5-preview" for m in result["codex"])
        assert not any(m["value"] == "gpt-4.5-preview-20250227" for m in result["codex"])

    @patch(
        "litellm.model_cost",
        {
            "haiku": {},
            "anthropic/haiku": {},
            "bedrock.haiku": {},
        },
    )
    def test_discover_models_excludes_scoped_names(self) -> None:
        from gobby.servers.routes.admin import _discover_models

        result = _discover_models()

        assert result["claude"] == [
            {"value": "", "label": "(default)"},
            {"value": "haiku", "label": "Haiku"}
        ]

    @patch(
        "litellm.model_cost",
        {
            "haiku": {},
            "claude-latest": {},
        },
    )
    def test_discover_models_excludes_latest_aliases(self) -> None:
        from gobby.servers.routes.admin import _discover_models

        result = _discover_models()

        assert any(m["value"] == "haiku" for m in result["claude"])
        assert not any(m["value"] == "claude-latest" for m in result.get("claude", []))

    @patch("litellm.model_cost", {})
    def test_discover_models_empty_registry(self) -> None:
        from gobby.servers.routes.admin import _discover_models

        result = _discover_models()
        assert result == {}

    @patch(
        "litellm.model_cost",
        {
            "llama-70b": {},
            "haiku": {},
        },
    )
    def test_discover_models_unknown_prefix_excluded(self) -> None:
        from gobby.servers.routes.admin import _discover_models

        result = _discover_models()

        # llama is not in _PROVIDER_PREFIXES
        assert "llama" not in result
        assert "claude" in result


class TestFallbackModelsFromConfig:
    """Tests for the _fallback_models_from_config helper function."""

    def test_fallback_returns_models_from_config(self) -> None:
        from gobby.servers.routes.admin import _fallback_models_from_config

        server = MagicMock()
        claude_config = MagicMock()
        claude_config.get_models_list.return_value = ["haiku"]
        gemini_config = MagicMock()
        gemini_config.get_models_list.return_value = ["gemini-2.0-flash"]

        server.services.config.llm_providers.claude = claude_config
        server.services.config.llm_providers.codex = None
        server.services.config.llm_providers.gemini = gemini_config
        server.services.config.llm_providers.litellm = None

        result = _fallback_models_from_config(server)

        assert result["claude"] == [
            {"value": "", "label": "(default)"},
            {"value": "haiku", "label": "Haiku"}
        ]
        assert result["gemini"] == [
            {"value": "", "label": "(default)"},
            {"value": "gemini-2.0-flash", "label": "Gemini 2.0 Flash"}
        ]
        assert "codex" not in result
        assert "litellm" not in result

    def test_fallback_no_config(self) -> None:
        from gobby.servers.routes.admin import _fallback_models_from_config

        server = MagicMock()
        server.services.config = None

        result = _fallback_models_from_config(server)
        assert result == {}

    def test_fallback_no_llm_providers(self) -> None:
        from gobby.servers.routes.admin import _fallback_models_from_config

        server = MagicMock()
        server.services.config.llm_providers = None

        result = _fallback_models_from_config(server)
        assert result == {}

    def test_fallback_empty_models_list(self) -> None:
        from gobby.servers.routes.admin import _fallback_models_from_config

        server = MagicMock()
        claude_config = MagicMock()
        claude_config.get_models_list.return_value = []

        server.services.config.llm_providers.claude = claude_config
        server.services.config.llm_providers.codex = None
        server.services.config.llm_providers.gemini = None
        server.services.config.llm_providers.litellm = None

        result = _fallback_models_from_config(server)
        assert "claude" not in result


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

    @patch("gobby.agents.registry.get_running_agent_registry")
    @patch("gobby.agents.registry.RunningAgent")
    def test_register_agent_success(self, mock_agent_cls, mock_get_registry, client) -> None:
        mock_registry = MagicMock()
        mock_get_registry.return_value = mock_registry

        mock_agent = MagicMock()
        mock_agent.to_dict.return_value = {
            "run_id": "run-1",
            "session_id": "sess-1",
            "parent_session_id": "parent-1",
            "mode": "terminal",
        }
        mock_agent_cls.return_value = mock_agent

        response = client.post(
            "/api/admin/test/register-agent",
            json={
                "run_id": "run-1",
                "session_id": "sess-1",
                "parent_session_id": "parent-1",
                "mode": "terminal",
            },
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["agent"]["run_id"] == "run-1"
        mock_registry.add.assert_called_once_with(mock_agent)

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

    @patch("gobby.agents.registry.get_running_agent_registry")
    def test_unregister_agent_success(self, mock_get_registry, client) -> None:
        mock_registry = MagicMock()
        mock_registry.remove.return_value = MagicMock()  # agent found
        mock_get_registry.return_value = mock_registry

        response = client.delete("/api/admin/test/unregister-agent/run-1")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert "run-1" in data["message"]
        mock_registry.remove.assert_called_once_with("run-1")

    @patch("gobby.agents.registry.get_running_agent_registry")
    def test_unregister_agent_not_found(self, mock_get_registry, client) -> None:
        mock_registry = MagicMock()
        mock_registry.remove.return_value = None  # agent not found
        mock_get_registry.return_value = mock_registry

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
                "total_cost_usd": 0.05,
            },
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["session_id"] == "sess-1"
        assert data["usage_set"]["input_tokens"] == 1000
        assert data["usage_set"]["output_tokens"] == 500
        assert data["usage_set"]["total_cost_usd"] == 0.05

        mock_server.session_manager.update_usage.assert_called_once_with(
            session_id="sess-1",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=200,
            cache_read_tokens=100,
            total_cost_usd=0.05,
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
        assert data["usage_set"]["total_cost_usd"] == 0.0

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
