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

    @patch("gobby.storage.agents.LocalAgentRunManager")
    def test_register_test_agent(self, mock_arm_cls, client, mock_server) -> None:
        """Test POST /test/register-agent endpoint."""
        mock_arm = MagicMock()
        mock_run = MagicMock()
        mock_run.to_dict.return_value = {
            "run_id": "ar-test",
            "id": "ar-test",
            "session_id": "sess-test",
            "parent_session_id": "parent-test",
            "mode": "interactive",
        }
        mock_arm.get.return_value = mock_run
        mock_arm_cls.return_value = mock_arm

        payload = {
            "run_id": "ar-test",
            "session_id": "sess-test",
            "parent_session_id": "parent-test",
            "mode": "interactive",
        }
        response = client.post("/api/admin/test/register-agent", json=payload)
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["agent"]["run_id"] == "ar-test"

        mock_arm.create.assert_called_once()
        mock_arm.start.assert_called_once_with("ar-test")
        mock_arm.update_child_session.assert_called_once_with("ar-test", "sess-test")

    @patch("gobby.storage.agents.LocalAgentRunManager")
    def test_register_test_agent_with_agent_name_and_terminal_status(
        self, mock_arm_cls, client, mock_server
    ) -> None:
        """Test POST /test/register-agent can seed terminal helper runs."""
        mock_arm = MagicMock()
        mock_run = MagicMock()
        mock_run.to_dict.return_value = {
            "run_id": "ar-memory",
            "agent_name": "memory-recall-helper",
            "status": "success",
        }
        mock_arm.get.return_value = mock_run
        mock_arm_cls.return_value = mock_arm

        response = client.post(
            "/api/admin/test/register-agent",
            json={
                "run_id": "ar-memory",
                "session_id": "helper-session",
                "parent_session_id": "parent-session",
                "agent_name": "memory-recall-helper",
                "status": "success",
            },
        )
        assert response.status_code == 200

        mock_arm.create.assert_called_once()
        assert mock_arm.create.call_args.kwargs["agent_name"] == "memory-recall-helper"
        mock_arm.start.assert_called_once_with("ar-memory")
        mock_arm.update_child_session.assert_called_once_with("ar-memory", "helper-session")
        mock_arm.complete.assert_called_once_with("ar-memory", result="test agent completed")

    def test_register_test_agent_rejects_unknown_status(self, client, mock_server) -> None:
        """Test POST /test/register-agent validates status."""
        response = client.post(
            "/api/admin/test/register-agent",
            json={
                "run_id": "ar-bad",
                "session_id": "helper-session",
                "parent_session_id": "parent-session",
                "status": "paused",
            },
        )
        assert response.status_code == 422

    @patch("gobby.storage.agents.LocalAgentRunManager")
    def test_unregister_test_agent(self, mock_arm_cls, client, mock_server) -> None:
        """Test DELETE /test/unregister-agent/{run_id} endpoint."""
        mock_arm = MagicMock()
        mock_arm.get.return_value = MagicMock()  # agent found
        mock_arm_cls.return_value = mock_arm

        response = client.delete("/api/admin/test/unregister-agent/ar-test")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        mock_arm.fail.assert_called_once_with("ar-test", error="Unregistered via test endpoint")

    def test_set_session_usage(self, client, mock_server) -> None:
        """Test POST /test/set-session-usage endpoint."""
        payload = {
            "session_id": "sess-123",
            "input_tokens": 100,
            "output_tokens": 50,
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
        )
