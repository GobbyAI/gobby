from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.servers.routes.admin import create_admin_router

pytestmark = pytest.mark.unit


class TestAdminRoutesExtended:
    @pytest.fixture
    def mock_server(self):
        server = MagicMock()
        server._start_time = 1234567890.0
        server._running = True
        server.port = 60887
        server.test_mode = True  # Enable test endpoints

        # Mock Daemon
        server._daemon = MagicMock()
        server._daemon.status.return_value = {"status": "running"}

        # Mock Managers
        server.mcp_manager = MagicMock()
        server.mcp_manager.server_configs = []

        server.llm_service = MagicMock()
        server.llm_service.enabled_providers = ["claude"]

        # Mock services config for models
        server.services = MagicMock()
        mock_provider_config = MagicMock()
        mock_provider_config.get_models_list.return_value = ["claude-3-opus", "claude-3-sonnet"]
        mock_provider_config.auth_mode = "api_key"
        server.services.config.llm_providers.claude = mock_provider_config

        # Mock internal manager for workflows/reload
        server._internal_manager = MagicMock()
        mock_registry = AsyncMock()
        mock_registry.name = "gobby-workflows"
        mock_registry.call.return_value = {"success": True, "count": 5}
        server._internal_manager.get_all_registries.return_value = [mock_registry]

        # Mock session manager for test endpoints
        server.session_manager = MagicMock()
        server.session_manager.db = MagicMock()  # Mock DB for project registration
        server.session_manager.update_usage.return_value = True

        return server

    @pytest.fixture
    def client(self, mock_server):
        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        return TestClient(app)

    def test_models_endpoint(self, client, mock_server) -> None:
        """Test GET /models endpoint with LiteLLM discovery."""
        mock_model_cost = {
            "gemini-2.5-pro": {},
            "gemini-2.5-flash": {},
            "gemini-1.5-pro": {},  # old generation — should be excluded
            "gpt-5.3-codex": {},
            "gpt-4o": {},  # retired generation — should be excluded
            "gpt-5.3-codex-20250601": {},  # dated variant — should be excluded
            "anthropic/claude-opus-4-6": {},  # provider-scoped — should be excluded
            "gemini-2.5-pro-image": {},  # image model — should be excluded
            "o3-pro": {},  # o1 prefix mapped to codex but o1 is excluded by min version
            "o4-mini": {},
        }
        mock_litellm = MagicMock()
        mock_litellm.model_cost = mock_model_cost
        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            response = client.get("/api/admin/models")

        assert response.status_code == 200
        data = response.json()

        # Gemini models returned as {value, label} dicts with (default) prepended
        assert "gemini" in data["models"]
        gemini = data["models"]["gemini"]
        gemini_values = [e["value"] for e in gemini]
        assert gemini[0] == {"value": "", "label": "(default)"}
        assert "gemini-2.5-pro" in gemini_values
        assert "gemini-2.5-flash" in gemini_values
        assert "gemini-1.5-pro" not in gemini_values  # old generation filtered
        assert "gemini-2.5-pro-image" not in gemini_values  # image model filtered

        # Codex (gpt/o3/o4) models
        assert "codex" in data["models"]
        codex = data["models"]["codex"]
        codex_values = [e["value"] for e in codex]
        assert codex[0] == {"value": "", "label": "(default)"}
        assert "gpt-5.3-codex" in codex_values
        assert "o3-pro" in codex_values
        assert "o4-mini" in codex_values
        assert "gpt-4o" not in codex_values  # retired
        assert "gpt-5.3-codex-20250601" not in codex_values  # dated variant

        assert data["default_model"] is not None

    def test_models_endpoint_provider_filter(self, client, mock_server) -> None:
        """Test GET /models?provider=gemini filters to Gemini only."""
        mock_model_cost = {
            "gemini-2.5-pro": {},
            "gemini-2.5-flash": {},
            "gpt-5.3-codex": {},
            "o4-mini": {},
        }
        mock_litellm = MagicMock()
        mock_litellm.model_cost = mock_model_cost
        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            response = client.get("/api/admin/models?provider=gemini")

        assert response.status_code == 200
        data = response.json()

        assert "gemini" in data["models"]
        assert "codex" not in data["models"]

    def test_reload_workflows_endpoint(self, client, mock_server) -> None:
        """Test POST /workflows/reload endpoint."""
        response = client.post("/api/admin/workflows/reload")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["message"] == "Workflow cache reloaded"

        # Verify call to internal registry
        registry = mock_server._internal_manager.get_all_registries.return_value[0]
        registry.call.assert_called_with("reload_cache", {})

    def test_register_test_project(self, client, mock_server) -> None:
        """Test POST /test/register-project endpoint."""
        # Mock checking for existing project (None)
        # Patch where the class is defined since it's imported locally in the function
        with patch("gobby.storage.projects.LocalProjectManager") as MockPM:
            mock_pm_instance = MockPM.return_value
            mock_pm_instance.get.return_value = None

            payload = {"project_id": "proj-123", "name": "Test Project", "repo_path": "/tmp/test"}
            response = client.post("/api/admin/test/register-project", json=payload)
            assert response.status_code == 200
            data = response.json()

            assert data["status"] == "success"
            assert data["project_id"] == "proj-123"

            # Verify DB insert
            mock_server.session_manager.db.execute.assert_called()

    @patch("gobby.agents.registry.get_running_agent_registry")
    def test_register_test_agent(self, mock_get_registry, client, mock_server) -> None:
        """Test POST /test/register-agent endpoint."""
        mock_registry = MagicMock()
        mock_get_registry.return_value = mock_registry

        payload = {
            "run_id": "ar-test",
            "session_id": "sess-test",
            "parent_session_id": "parent-test",
            "mode": "terminal",
        }
        response = client.post("/api/admin/test/register-agent", json=payload)
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["agent"]["run_id"] == "ar-test"

        mock_registry.add.assert_called()

    @patch("gobby.agents.registry.get_running_agent_registry")
    def test_unregister_test_agent(self, mock_get_registry, client, mock_server) -> None:
        """Test DELETE /test/unregister-agent/{run_id} endpoint."""
        mock_registry = MagicMock()
        mock_agent = MagicMock()
        mock_registry.remove.return_value = mock_agent
        mock_get_registry.return_value = mock_registry

        response = client.delete("/api/admin/test/unregister-agent/ar-test")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        mock_registry.remove.assert_called_with("ar-test")

    def test_set_session_usage(self, client, mock_server) -> None:
        """Test POST /test/set-session-usage endpoint."""
        payload = {
            "session_id": "sess-123",
            "input_tokens": 100,
            "output_tokens": 50,
            "total_cost_usd": 0.05,
        }
        response = client.post("/api/admin/test/set-session-usage", json=payload)
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["usage_set"]["input_tokens"] == 100

        mock_server.session_manager.update_usage.assert_called_with(
            session_id="sess-123",
            input_tokens=100,
            output_tokens=50,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            total_cost_usd=0.05,
        )
