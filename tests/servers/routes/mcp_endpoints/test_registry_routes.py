"""Tests for MCP registry endpoints (embed, status, refresh)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from gobby.mcp_proxy.client_manager import server_registry as server_registry_mod
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPServerConfig
from gobby.mcp_proxy.services.server_mgmt import ServerManagementService
from gobby.servers.routes.dependencies import get_metrics_manager, get_server
from gobby.servers.routes.mcp.tools import create_mcp_router
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import GLOBAL_PROJECT_ID
from tests.mcp_proxy.services.test_scope_resolution_matrix import PROJECT_ID

pytestmark = pytest.mark.unit


class TestMCPRegistryRoutes:
    @pytest.fixture
    def mock_server(self) -> MagicMock:
        server = MagicMock()
        server.mcp_manager = None
        server._internal_manager = None
        server._tools_handler = None
        server._mcp_db_manager = None
        server.resolve_project_id.return_value = "proj-1"
        return server

    @pytest.fixture
    def client(self, mock_server: MagicMock) -> TestClient:
        app = FastAPI()
        router = create_mcp_router()
        app.include_router(router)

        async def override_server():
            return mock_server

        async def override_metrics():
            return None

        app.dependency_overrides[get_server] = override_server
        app.dependency_overrides[get_metrics_manager] = override_metrics
        return TestClient(app)

    # -----------------------------------------------------------------
    # POST /mcp/tools/embed
    # -----------------------------------------------------------------

    def test_embed_no_semantic_search(self, client: TestClient, mock_server: MagicMock) -> None:
        response = client.post("/api/mcp/tools/embed", json={"cwd": "/tmp/proj"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not configured" in data["error"]

    def test_embed_project_resolve_fail(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server.resolve_project_id.side_effect = ValueError("No project")

        response = client.post("/api/mcp/tools/embed", json={"cwd": "/tmp"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "No project" in data["error"]

    def test_embed_success(self, client: TestClient, mock_server: MagicMock) -> None:
        semantic_search = MagicMock()
        semantic_search.embed_all_tools = AsyncMock(
            return_value={"tools_embedded": 5, "servers_processed": 2}
        )
        mock_server._tools_handler = MagicMock()
        mock_server._tools_handler._semantic_search = semantic_search

        response = client.post("/api/mcp/tools/embed", json={"cwd": "/tmp/proj", "force": True})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["stats"]["tools_embedded"] == 5

    def test_embed_failure(self, client: TestClient, mock_server: MagicMock) -> None:
        semantic_search = MagicMock()
        semantic_search.embed_all_tools = AsyncMock(side_effect=RuntimeError("LLM down"))
        mock_server._tools_handler = MagicMock()
        mock_server._tools_handler._semantic_search = semantic_search

        response = client.post("/api/mcp/tools/embed", json={"cwd": "/tmp"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "LLM down" in data["error"]

    def test_embed_tools_handler_no_semantic(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """tools_handler exists but _semantic_search is None."""
        mock_server._tools_handler = MagicMock()
        mock_server._tools_handler._semantic_search = None

        response = client.post("/api/mcp/tools/embed", json={"cwd": "/tmp"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not configured" in data["error"]

    def test_embed_general_exception(self, client: TestClient, mock_server: MagicMock) -> None:
        """Outer exception handler for embed_mcp_tools."""
        # Make request.json() work but resolve_project_id blow up with non-ValueError
        mock_server.resolve_project_id.side_effect = RuntimeError("catastrophic")

        response = client.post("/api/mcp/tools/embed", json={"cwd": "/tmp"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "catastrophic" in data["error"]

    # -----------------------------------------------------------------
    # GET /mcp/status
    # -----------------------------------------------------------------

    def test_status_empty(self, client: TestClient) -> None:
        response = client.get("/api/mcp/status")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["total_servers"] == 0
        assert data["connected_servers"] == 0
        assert data["cached_tools"] == 0

    def test_status_with_internal_servers(self, client: TestClient, mock_server: MagicMock) -> None:
        registry = MagicMock()
        registry.name = "gobby-tasks"
        registry.list_tools.return_value = [
            {"name": "t1"},
            {"name": "t2"},
        ]
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.return_value = [registry]

        response = client.get("/api/mcp/status")
        assert response.status_code == 200
        data = response.json()
        assert data["total_servers"] == 1
        assert data["connected_servers"] == 1
        assert data["cached_tools"] == 2
        assert data["server_health"]["gobby-tasks"]["state"] == "connected"
        assert data["server_health"]["gobby-tasks"]["health"] == "healthy"
        assert data["server_health"]["gobby-tasks"]["failures"] == 0

    def test_status_with_external_connected(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        config = MagicMock()
        config.name = "github"
        health = MagicMock()
        health.state.value = "connected"
        health.health.value = "healthy"
        health.consecutive_failures = 0

        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.server_configs = [config]
        mock_server.mcp_manager.health = {"github": health}
        mock_server.mcp_manager.connections = {"github": MagicMock()}

        response = client.get("/api/mcp/status")
        assert response.status_code == 200
        data = response.json()
        assert data["total_servers"] == 1
        assert data["connected_servers"] == 1
        assert data["server_health"]["github"]["state"] == "connected"

    def test_status_disconnected_external(self, client: TestClient, mock_server: MagicMock) -> None:
        config = MagicMock()
        config.name = "github"
        health = MagicMock()
        health.state.value = "disconnected"
        health.health.value = "unhealthy"
        health.consecutive_failures = 3

        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.server_configs = [config]
        mock_server.mcp_manager.health = {"github": health}
        mock_server.mcp_manager.connections = {}  # Not connected

        response = client.get("/api/mcp/status")
        assert response.status_code == 200
        data = response.json()
        assert data["total_servers"] == 1
        assert data["connected_servers"] == 0
        assert data["server_health"]["github"]["failures"] == 3

    def test_status_failed_transport_is_not_reported_connected(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        config = MCPServerConfig(
            name="failed-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )
        manager = MCPClientManager(server_configs=[config])
        failed_connection = MagicMock()
        failed_connection.is_connected = False
        manager._connections[config.name] = failed_connection
        mock_server.mcp_manager = manager

        response = client.get("/api/mcp/status")

        assert response.status_code == 200
        data = response.json()
        assert data["total_servers"] == 1
        assert data["connected_servers"] == 0

    def test_status_external_no_health(self, client: TestClient, mock_server: MagicMock) -> None:
        """External server with no health data."""
        config = MagicMock()
        config.name = "unknown-server"

        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.server_configs = [config]
        mock_server.mcp_manager.health = {}  # No health for this server
        mock_server.mcp_manager.connections = {}

        response = client.get("/api/mcp/status")
        assert response.status_code == 200
        data = response.json()
        assert data["server_health"]["unknown-server"]["state"] == "unknown"
        assert data["server_health"]["unknown-server"]["health"] == "unknown"
        assert data["server_health"]["unknown-server"]["failures"] == 0

    def test_status_mixed_servers(self, client: TestClient, mock_server: MagicMock) -> None:
        """Internal and external servers together."""
        # Internal
        registry = MagicMock()
        registry.name = "gobby-tasks"
        registry.list_tools.return_value = [{"name": "t1"}]
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.return_value = [registry]

        # External
        ext_config = MagicMock()
        ext_config.name = "github"
        health = MagicMock()
        health.state.value = "connected"
        health.health.value = "healthy"
        health.consecutive_failures = 0
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.server_configs = [ext_config]
        mock_server.mcp_manager.health = {"github": health}
        mock_server.mcp_manager.connections = {"github": MagicMock()}

        response = client.get("/api/mcp/status")
        assert response.status_code == 200
        data = response.json()
        assert data["total_servers"] == 2
        assert data["connected_servers"] == 2
        assert data["cached_tools"] == 1

    def test_status_error(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.side_effect = RuntimeError("boom")

        response = client.get("/api/mcp/status")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False

    # -----------------------------------------------------------------
    # POST /mcp/refresh
    # -----------------------------------------------------------------

    def test_refresh_no_db_manager(self, client: TestClient, mock_server: MagicMock) -> None:
        response = client.post("/api/mcp/refresh", json={"cwd": "/tmp"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not configured" in data["error"]

    def test_refresh_project_resolve_fail(self, client: TestClient, mock_server: MagicMock) -> None:
        response = client.post("/api/mcp/refresh", json={"scope": "project"})
        assert response.status_code == 400
        data = response.json()
        detail = data.get("detail", data)
        assert detail.get("error") == "project_scope_unresolved"

    def test_refresh_no_servers(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server._mcp_db_manager = MagicMock()
        mock_server._mcp_db_manager.db = MagicMock()

        response = client.post("/api/mcp/refresh", json={"cwd": "/tmp"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["stats"]["servers_processed"] == 0

    def test_refresh_internal_server_force_mode(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """Refresh internal server with force=True (all tools treated as new)."""
        mock_server._mcp_db_manager = MagicMock()
        mock_server._mcp_db_manager.db = MagicMock()

        # Internal registry
        registry = MagicMock()
        registry.name = "gobby-tasks"
        registry.list_tools.return_value = [
            {"name": "create_task", "description": "Create a task"},
            {"name": "list_tasks", "description": "List tasks"},
        ]
        registry.get_schema.return_value = {"type": "object", "properties": {}}

        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.return_value = [registry]
        mock_server._internal_manager.is_internal.return_value = True
        mock_server._internal_manager.get_registry.return_value = registry

        # No semantic search (no embeddings)
        mock_server._tools_handler = None

        with (
            patch("gobby.mcp_proxy.schema_hash.SchemaHashManager") as MockSHM,
            patch("gobby.mcp_proxy.schema_hash.compute_schema_hash") as mock_hash,
        ):
            mock_shm_instance = MockSHM.return_value
            mock_shm_instance.cleanup_stale_hashes.return_value = 0
            mock_hash.return_value = "abc123"

            response = client.post(
                "/api/mcp/refresh",
                json={"cwd": "/tmp", "force": True},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["force"] is True
        stats = data["stats"]
        assert stats["servers_processed"] == 1
        assert stats["tools_new"] == 2
        assert stats["tools_changed"] == 0

        # Verify by_server
        assert "gobby-tasks" in stats["by_server"]
        assert stats["by_server"]["gobby-tasks"]["new"] == 2

    def test_refresh_internal_server_check_changes(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """Refresh internal server with force=False (schema change detection)."""
        mock_server._mcp_db_manager = MagicMock()
        mock_server._mcp_db_manager.db = MagicMock()

        registry = MagicMock()
        registry.name = "gobby-tasks"
        registry.list_tools.return_value = [
            {"name": "create_task", "description": "Create"},
            {"name": "unchanged_tool", "description": "Unchanged"},
        ]
        registry.get_schema.return_value = {"type": "object"}

        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.return_value = [registry]
        mock_server._internal_manager.is_internal.return_value = True
        mock_server._internal_manager.get_registry.return_value = registry

        with (
            patch("gobby.mcp_proxy.schema_hash.SchemaHashManager") as MockSHM,
            patch("gobby.mcp_proxy.schema_hash.compute_schema_hash") as mock_hash,
        ):
            mock_shm_instance = MockSHM.return_value
            mock_shm_instance.check_tools_for_changes.return_value = {
                "new": ["create_task"],
                "changed": [],
                "unchanged": ["unchanged_tool"],
            }
            mock_shm_instance.cleanup_stale_hashes.return_value = 1
            mock_hash.return_value = "newhash"

            response = client.post(
                "/api/mcp/refresh",
                json={"cwd": "/tmp", "force": False},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["force"] is False
        stats = data["stats"]
        assert stats["tools_new"] == 1
        assert stats["tools_unchanged"] == 1
        assert stats["tools_removed"] == 1

    def test_refresh_with_server_filter(self, client: TestClient, mock_server: MagicMock) -> None:
        """Refresh only processes servers matching the filter."""
        mock_server._mcp_db_manager = MagicMock()
        mock_server._mcp_db_manager.db = MagicMock()

        # Two internal registries
        reg1 = MagicMock()
        reg1.name = "gobby-tasks"
        reg2 = MagicMock()
        reg2.name = "gobby-memory"

        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.return_value = [reg1, reg2]
        mock_server._internal_manager.is_internal.return_value = True

        # Filter to only gobby-tasks
        reg1.list_tools.return_value = [{"name": "t1", "description": "Tool 1"}]
        reg1.get_schema.return_value = {}
        mock_server._internal_manager.get_registry.return_value = reg1

        with (
            patch("gobby.mcp_proxy.schema_hash.SchemaHashManager") as MockSHM,
            patch("gobby.mcp_proxy.schema_hash.compute_schema_hash"),
        ):
            mock_shm_instance = MockSHM.return_value
            mock_shm_instance.check_tools_for_changes.return_value = {
                "new": [],
                "changed": [],
                "unchanged": ["t1"],
            }
            mock_shm_instance.cleanup_stale_hashes.return_value = 0

            response = client.post(
                "/api/mcp/refresh",
                json={"cwd": "/tmp", "server": "gobby-tasks"},
            )

        assert response.status_code == 200
        data = response.json()
        stats = data["stats"]
        assert stats["servers_processed"] == 1
        assert "gobby-tasks" in stats["by_server"]
        assert "gobby-memory" not in stats["by_server"]

    def test_refresh_external_server(self, client: TestClient, mock_server: MagicMock) -> None:
        """Refresh processes external MCP servers."""
        mock_server._mcp_db_manager = MagicMock()
        mock_server._mcp_db_manager.db = MagicMock()

        ext_config = MagicMock()
        ext_config.name = "github-mcp"
        ext_config.enabled = True
        ext_config.tools = []
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.server_configs = [ext_config]

        mock_tool = MagicMock()
        mock_tool.name = "list_repos"
        mock_tool.description = "List GitHub repos"
        mock_tool.input_schema = {"type": "object", "properties": {"org": {"type": "string"}}}
        mock_session = AsyncMock()
        mock_tools_result = MagicMock()
        mock_tools_result.tools = [mock_tool]
        mock_session.list_tools.return_value = mock_tools_result

        async def ensure_connected(_server_id: object) -> AsyncMock:
            ext_config.tools = [{"name": "list_repos", "brief": "List GitHub repos"}]
            return mock_session

        mock_server.mcp_manager.ensure_connected = AsyncMock(side_effect=ensure_connected)
        mock_server.mcp_manager.refresh_server = AsyncMock()

        with (
            patch("gobby.mcp_proxy.schema_hash.SchemaHashManager") as MockSHM,
            patch("gobby.mcp_proxy.schema_hash.compute_schema_hash") as mock_hash,
        ):
            mock_shm_instance = MockSHM.return_value
            mock_shm_instance.check_tools_for_changes.return_value = {
                "new": ["list_repos"],
                "changed": [],
                "unchanged": [],
            }
            mock_shm_instance.cleanup_stale_hashes.return_value = 0
            mock_hash.return_value = "hash123"

            response = client.post(
                "/api/mcp/refresh",
                json={"cwd": "/tmp"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["stats"]["servers_processed"] == 1
        assert "github-mcp" in data["stats"]["by_server"]
        mock_server.mcp_manager.refresh_server.assert_called()
        mock_server.mcp_manager.ensure_connected.assert_awaited()

        inventory_response = client.get("/api/mcp/tools")

        assert inventory_response.status_code == 200
        assert inventory_response.json()["tools"]["github-mcp"] == [
            {"name": "list_repos", "brief": "List GitHub repos"}
        ]
        mock_server.mcp_manager.ensure_connected.assert_awaited_once_with("github-mcp")

    def test_refresh_external_server_connection_error(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """External server connection failure records error in stats."""
        mock_server._mcp_db_manager = MagicMock()
        mock_server._mcp_db_manager.db = MagicMock()

        ext_config = MagicMock()
        ext_config.name = "broken-server"
        ext_config.enabled = True
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.server_configs = [ext_config]
        mock_server.mcp_manager.ensure_connected = AsyncMock(side_effect=ConnectionError("refused"))

        with patch("gobby.mcp_proxy.schema_hash.SchemaHashManager"):
            response = client.post(
                "/api/mcp/refresh",
                json={"cwd": "/tmp"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "broken-server" in data["stats"]["by_server"]
        assert "error" in data["stats"]["by_server"]["broken-server"]

    def test_refresh_with_semantic_search_embeddings(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """Refresh generates embeddings for new/changed tools when semantic search available."""
        mock_server._mcp_db_manager = MagicMock()
        mock_server._mcp_db_manager.db = MagicMock()

        registry = MagicMock()
        registry.name = "gobby-tasks"
        registry.list_tools.return_value = [
            {"name": "new_tool", "description": "New tool"},
        ]
        registry.get_schema.return_value = {"type": "object"}

        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.return_value = [registry]
        mock_server._internal_manager.is_internal.return_value = True
        mock_server._internal_manager.get_registry.return_value = registry

        # Set up semantic search
        semantic_search = MagicMock()
        semantic_search.embed_tool = AsyncMock()
        mock_server._tools_handler = MagicMock()
        mock_server._tools_handler._semantic_search = semantic_search

        with (
            patch("gobby.mcp_proxy.schema_hash.SchemaHashManager") as MockSHM,
            patch("gobby.mcp_proxy.schema_hash.compute_schema_hash") as mock_hash,
        ):
            mock_shm_instance = MockSHM.return_value
            mock_shm_instance.check_tools_for_changes.return_value = {
                "new": ["new_tool"],
                "changed": [],
                "unchanged": [],
            }
            mock_shm_instance.cleanup_stale_hashes.return_value = 0
            mock_hash.return_value = "newhash"

            response = client.post(
                "/api/mcp/refresh",
                json={"cwd": "/tmp"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["stats"]["embeddings_generated"] == 1
        assert data["stats"]["by_server"]["gobby-tasks"]["embeddings"] == 1
        semantic_search.embed_tool.assert_called_once()

    def test_refresh_embedding_error_does_not_fail(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """Embedding failure is logged but doesn't fail the refresh."""
        mock_server._mcp_db_manager = MagicMock()
        mock_server._mcp_db_manager.db = MagicMock()

        registry = MagicMock()
        registry.name = "gobby-tasks"
        registry.list_tools.return_value = [
            {"name": "tool1", "description": "Tool"},
        ]
        registry.get_schema.return_value = {}

        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.return_value = [registry]
        mock_server._internal_manager.is_internal.return_value = True
        mock_server._internal_manager.get_registry.return_value = registry

        semantic_search = MagicMock()
        semantic_search.embed_tool = AsyncMock(side_effect=RuntimeError("embed failed"))
        mock_server._tools_handler = MagicMock()
        mock_server._tools_handler._semantic_search = semantic_search

        with (
            patch("gobby.mcp_proxy.schema_hash.SchemaHashManager") as MockSHM,
            patch("gobby.mcp_proxy.schema_hash.compute_schema_hash"),
        ):
            mock_shm_instance = MockSHM.return_value
            mock_shm_instance.check_tools_for_changes.return_value = {
                "new": ["tool1"],
                "changed": [],
                "unchanged": [],
            }
            mock_shm_instance.cleanup_stale_hashes.return_value = 0

            response = client.post(
                "/api/mcp/refresh",
                json={"cwd": "/tmp"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        # Embedding failed - should be 0
        assert data["stats"]["embeddings_generated"] == 0

    def test_refresh_changed_tools_schema_update(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """Changed tools get schema hash updated."""
        mock_server._mcp_db_manager = MagicMock()
        mock_server._mcp_db_manager.db = MagicMock()

        registry = MagicMock()
        registry.name = "gobby-tasks"
        registry.list_tools.return_value = [
            {"name": "changed_tool", "description": "Changed"},
            {"name": "same_tool", "description": "Same"},
        ]
        registry.get_schema.return_value = {"type": "object"}

        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.return_value = [registry]
        mock_server._internal_manager.is_internal.return_value = True
        mock_server._internal_manager.get_registry.return_value = registry

        with (
            patch("gobby.mcp_proxy.schema_hash.SchemaHashManager") as MockSHM,
            patch("gobby.mcp_proxy.schema_hash.compute_schema_hash") as mock_hash,
        ):
            mock_shm_instance = MockSHM.return_value
            mock_shm_instance.check_tools_for_changes.return_value = {
                "new": [],
                "changed": ["changed_tool"],
                "unchanged": ["same_tool"],
            }
            mock_shm_instance.cleanup_stale_hashes.return_value = 0
            mock_hash.return_value = "changed_hash"

            response = client.post(
                "/api/mcp/refresh",
                json={"cwd": "/tmp"},
            )

        assert response.status_code == 200
        data = response.json()
        stats = data["stats"]
        assert stats["tools_changed"] == 1
        assert stats["tools_unchanged"] == 1

        # Verify store_hash was called for changed tool
        mock_shm_instance.store_hash.assert_called_once()
        # Verify update_verification_time was called for unchanged tool
        mock_shm_instance.update_verification_time.assert_called_once()

    def test_refresh_server_processing_error(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """Exception while processing a specific server records error in stats."""
        mock_server._mcp_db_manager = MagicMock()
        mock_server._mcp_db_manager.db = MagicMock()

        registry = MagicMock()
        registry.name = "broken-registry"
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.return_value = [registry]
        mock_server._internal_manager.is_internal.return_value = True
        mock_server._internal_manager.get_registry.return_value = registry
        registry.list_tools.side_effect = RuntimeError("registry boom")

        with patch("gobby.mcp_proxy.schema_hash.SchemaHashManager"):
            response = client.post(
                "/api/mcp/refresh",
                json={"cwd": "/tmp"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "broken-registry" in data["stats"]["by_server"]
        assert "error" in data["stats"]["by_server"]["broken-registry"]

    def test_refresh_general_exception(self, client: TestClient, mock_server: MagicMock) -> None:
        """Outer exception handler."""
        mock_server._mcp_db_manager = MagicMock()
        mock_server._mcp_db_manager.db = MagicMock()
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.side_effect = RuntimeError(
            "registry error"
        )

        response = client.post("/api/mcp/refresh", json={"cwd": "/tmp"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "registry error" in data["error"]

    def test_refresh_external_disabled_servers_skipped(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """Disabled external servers are skipped during refresh."""
        mock_server._mcp_db_manager = MagicMock()
        mock_server._mcp_db_manager.db = MagicMock()

        ext_config = MagicMock()
        ext_config.name = "disabled-server"
        ext_config.enabled = False
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.server_configs = [ext_config]

        with patch("gobby.mcp_proxy.schema_hash.SchemaHashManager"):
            response = client.post(
                "/api/mcp/refresh",
                json={"cwd": "/tmp"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["stats"]["servers_processed"] == 0

    def test_refresh_external_tool_with_model_dump_schema(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """External tool with inputSchema that has model_dump()."""
        mock_server._mcp_db_manager = MagicMock()
        mock_server._mcp_db_manager.db = MagicMock()

        ext_config = MagicMock()
        ext_config.name = "ext-server"
        ext_config.enabled = True
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.server_configs = [ext_config]

        # Tool with inputSchema that has model_dump
        mock_tool = MagicMock()
        mock_tool.name = "tool_with_pydantic_schema"
        mock_tool.description = "Has pydantic schema"
        mock_input_schema = MagicMock()
        mock_input_schema.model_dump.return_value = {
            "type": "object",
            "properties": {"x": {"type": "string"}},
        }
        mock_tool.input_schema = mock_input_schema

        mock_session = AsyncMock()
        mock_tools_result = MagicMock()
        mock_tools_result.tools = [mock_tool]
        mock_session.list_tools.return_value = mock_tools_result
        mock_server.mcp_manager.ensure_connected = AsyncMock(return_value=mock_session)

        with (
            patch("gobby.mcp_proxy.schema_hash.SchemaHashManager") as MockSHM,
            patch("gobby.mcp_proxy.schema_hash.compute_schema_hash") as mock_hash,
        ):
            mock_shm_instance = MockSHM.return_value
            mock_shm_instance.check_tools_for_changes.return_value = {
                "new": ["tool_with_pydantic_schema"],
                "changed": [],
                "unchanged": [],
            }
            mock_shm_instance.cleanup_stale_hashes.return_value = 0
            mock_hash.return_value = "schema_hash"

            response = client.post(
                "/api/mcp/refresh",
                json={"cwd": "/tmp"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["stats"]["tools_new"] == 1

    def test_refresh_external_tool_with_dict_schema(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """External tool with inputSchema that is a plain dict."""
        mock_server._mcp_db_manager = MagicMock()
        mock_server._mcp_db_manager.db = MagicMock()

        ext_config = MagicMock()
        ext_config.name = "ext-server"
        ext_config.enabled = True
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.server_configs = [ext_config]

        mock_tool = MagicMock()
        mock_tool.name = "tool_with_dict_schema"
        mock_tool.description = "Has dict schema"
        # inputSchema is a plain dict (no model_dump)
        mock_tool.input_schema = {"type": "object", "properties": {}}

        mock_session = AsyncMock()
        mock_tools_result = MagicMock()
        mock_tools_result.tools = [mock_tool]
        mock_session.list_tools.return_value = mock_tools_result
        mock_server.mcp_manager.ensure_connected = AsyncMock(return_value=mock_session)

        with (
            patch("gobby.mcp_proxy.schema_hash.SchemaHashManager") as MockSHM,
            patch("gobby.mcp_proxy.schema_hash.compute_schema_hash") as mock_hash,
        ):
            mock_shm_instance = MockSHM.return_value
            mock_shm_instance.check_tools_for_changes.return_value = {
                "new": [],
                "changed": [],
                "unchanged": ["tool_with_dict_schema"],
            }
            mock_shm_instance.cleanup_stale_hashes.return_value = 0
            mock_hash.return_value = "dict_hash"

            response = client.post(
                "/api/mcp/refresh",
                json={"cwd": "/tmp"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True


def _demo_template() -> dict[str, Any]:
    return {
        "name": "demo",
        "description": "Demo MCP template",
        "version": 1,
        "transport": "stdio",
        "command": "uvx",
        "args": ["demo"],
        "params": [
            {"name": "region", "env": "REGION", "required": True},
            {
                "name": "mode",
                "env": "MODE",
                "required": False,
                "choices": ["fast", "slow"],
            },
            {"name": "tag", "arg_flag": "--tag", "required": False},
        ],
    }


def _mcp_app(http_server: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(create_mcp_router())

    async def override_server() -> Any:
        return http_server

    app.dependency_overrides[get_server] = override_server
    app.dependency_overrides[get_metrics_manager] = lambda: None
    return app


def _http_server_for(manager: Any) -> MagicMock:
    server = MagicMock()
    server.mcp_manager = manager
    server._internal_manager = None
    server._tools_handler = None
    server._mcp_db_manager = getattr(manager, "mcp_db_manager", None)
    server.session_manager = None
    server.config = MagicMock()
    server.llm_service = None
    server.tool_proxy = None
    server.services = MagicMock()
    server.services.websocket_server = None
    server.services.database = getattr(getattr(manager, "mcp_db_manager", None), "db", None)
    return server


async def _add_templated_instance(
    temp_db: HubDatabase,
    project_id: str,
    *,
    values: dict[str, str] | None = None,
    name: str = "demo-instance",
) -> tuple[MCPClientManager, dict[str, Any]]:
    storage = LocalMCPManager(temp_db)
    storage.upsert_template(
        name="demo",
        project_id=project_id,
        owner="user",
        definition=_demo_template(),
        enabled=True,
    )
    manager = MCPClientManager(
        server_configs=[],
        project_id=project_id,
        mcp_db_manager=storage,
        lazy_connect=True,
    )
    service = ServerManagementService(manager, config_manager=MagicMock())
    added = await service.add_server(
        name,
        template="demo",
        values=values or {"region": "us", "mode": "fast", "tag": "alpha"},
        scope="project",
        project_id=project_id,
        enabled=False,
    )
    assert added["success"] is True
    return manager, added


def _detail(response: Any) -> dict[str, Any]:
    payload = response.json()
    if isinstance(payload, dict) and isinstance(payload.get("detail"), dict):
        return cast(dict[str, Any], payload["detail"])
    return cast(dict[str, Any], payload if isinstance(payload, dict) else {})


def _storage(manager: MCPClientManager) -> LocalMCPManager:
    db = manager.mcp_db_manager
    assert isinstance(db, LocalMCPManager)
    return db


def _template_values(row: Any) -> dict[str, Any]:
    values = getattr(row, "template_values", None)
    assert isinstance(values, dict)
    return values


def test_project_scope_precedence_and_web_legacy_payload(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project_id = sample_project["id"]
    manager = MCPClientManager(
        server_configs=[],
        project_id=project_id,
        mcp_db_manager=LocalMCPManager(temp_db),
        lazy_connect=True,
    )
    server = _http_server_for(manager)
    client = TestClient(_mcp_app(server))

    web = client.post(
        "/api/mcp/servers",
        json={
            "name": "web-tab",
            "transport": "http",
            "url": "https://web.example.test/mcp",
            "command": None,
            "args": None,
            "env": {},
            "enabled": False,
            "project_id": "",
        },
    )
    web_body = web.json()
    assert web.status_code == 200
    assert web_body["success"] is True
    listed = client.get("/api/mcp/servers")
    servers = listed.json()["servers"]
    web_row = next(row for row in servers if row["name"] == "web-tab")
    assert web_row["scope"] == "global"
    assert web_row["project_id"] == GLOBAL_PROJECT_ID
    assert "id" in web_row
    assert "missing_secrets" in web_row

    scoped = client.post(
        "/api/mcp/servers",
        json={
            "name": "project-tab",
            "transport": "http",
            "url": "https://project.example.test/mcp",
            "enabled": False,
            "project_id": project_id,
        },
    )
    assert scoped.json()["success"] is True
    assert scoped.json()["scope"] == "project"

    missing = client.post(
        "/api/mcp/servers",
        json={
            "name": "missing-project",
            "transport": "http",
            "url": "https://x.example.test/mcp",
            "enabled": False,
            "scope": "project",
        },
    )
    assert missing.status_code == 400
    assert _detail(missing)["error"] == "project_scope_unresolved"

    unknown = client.post(
        "/api/mcp/servers",
        json={
            "name": "unknown-project",
            "transport": "http",
            "url": "https://x.example.test/mcp",
            "enabled": False,
            "project_id": "00000000-0000-4000-8000-000000000099",
        },
    )
    assert unknown.status_code == 400
    assert _detail(unknown)["error"] == "project_scope_unresolved"


def test_import_mcp_server_respects_project_and_global_scope(
    sample_project: dict[str, Any],
) -> None:
    project_id = sample_project["id"]
    importer = MagicMock()
    importer.import_from_project = AsyncMock(
        return_value={"success": True, "imported": ["context7"], "project_id": project_id}
    )
    server = MagicMock()
    server.mcp_manager = MagicMock()
    server.config = MagicMock()
    server.llm_service = None
    server.services.database = MagicMock()
    server.services.websocket_server = None
    server.session_manager = None
    client = TestClient(_mcp_app(server))

    with patch(
        "gobby.mcp_proxy.importer.MCPServerImporter",
        return_value=importer,
    ) as importer_cls:
        project = client.post(
            "/api/mcp/servers/import",
            json={"from_project": "other", "project_id": project_id},
        )
        global_scope = client.post(
            "/api/mcp/servers/import",
            json={"from_project": "other", "scope": "global"},
        )

    assert project.json()["success"] is True
    assert global_scope.json()["success"] is True
    assert importer.import_from_project.await_count == 2
    assert importer_cls.call_args_list[0].kwargs["current_project_id"] == project_id
    assert importer_cls.call_args_list[1].kwargs["current_project_id"] == GLOBAL_PROJECT_ID


@pytest.mark.asyncio
async def test_update_mcp_server_preserves_identity_and_rejects_template_owned_fields(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project_id = sample_project["id"]
    manager, added = await _add_templated_instance(temp_db, project_id)
    server_id = added["id"]
    storage = _storage(manager)
    client = TestClient(_mcp_app(_http_server_for(manager)))

    owned = client.patch(
        "/api/mcp/servers/demo-instance",
        json={
            "command": "npx",
            "project_id": project_id,
            "values": {"region": "eu"},
        },
    )
    assert owned.status_code == 400
    detail = _detail(owned)
    assert detail["error"] == "template_owned_fields"
    fields = detail.get("fields") or detail.get("template_owned_fields") or []
    assert "command" in fields

    updated = client.patch(
        "/api/mcp/servers/demo-instance",
        json={"values": {"region": "eu"}, "project_id": project_id},
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["success"] is True
    assert body.get("id") == server_id
    row = storage.get_server("demo-instance", project_id)
    assert row is not None
    assert row.id == server_id
    assert str(row.project_id) == project_id
    assert row.template == "demo"
    assert _template_values(row)["region"] == "eu"
    assert row.command == "uvx"
    refreshed = storage.refresh_template_instances(
        lambda _template, server: {
            "transport": server.transport,
            "url": server.url,
            "command": server.command,
            "args": server.args,
            "env": server.env,
            "headers": server.headers,
            "connect_timeout": server.connect_timeout,
            "runtime_hook": server.runtime_hook,
        },
        server_id=server_id,
    )
    assert refreshed["refreshed"] == 1
    after = storage.get_server_by_id(server_id)
    assert after is not None
    assert after.command == "uvx"
    assert after.env is not None
    assert after.env["REGION"] == "eu"


@pytest.mark.asyncio
async def test_update_mcp_server_merges_values_and_null_removes_parameter(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project_id = sample_project["id"]
    manager, _added = await _add_templated_instance(temp_db, project_id)
    storage = _storage(manager)
    client = TestClient(_mcp_app(_http_server_for(manager)))

    merged = client.patch(
        "/api/mcp/servers/demo-instance",
        json={"values": {"region": "eu"}, "project_id": project_id},
    )
    assert merged.status_code == 200
    row = storage.get_server("demo-instance", project_id)
    assert row is not None
    assert _template_values(row)["region"] == "eu"
    assert _template_values(row)["mode"] == "fast"
    assert row.env is not None
    assert row.env["REGION"] == "eu"
    assert row.env["MODE"] == "fast"

    removed = client.patch(
        "/api/mcp/servers/demo-instance",
        json={"values": {"tag": None}, "project_id": project_id},
    )
    assert removed.status_code == 200
    row = storage.get_server("demo-instance", project_id)
    assert row is not None
    assert "tag" not in _template_values(row)
    assert "--tag" not in (row.args or [])

    invalid = client.patch(
        "/api/mcp/servers/demo-instance",
        json={"values": {"mode": "invalid"}, "project_id": project_id},
    )
    assert invalid.status_code == 400
    assert _detail(invalid)["error"] == "template_values_invalid"
    row = storage.get_server("demo-instance", project_id)
    assert row is not None
    assert _template_values(row)["mode"] == "fast"


@pytest.mark.asyncio
async def test_project_scoped_mutations_never_fall_back_to_global(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project_id = sample_project["id"]
    storage = LocalMCPManager(temp_db)
    manager = MCPClientManager(
        server_configs=[],
        project_id=project_id,
        mcp_db_manager=storage,
        lazy_connect=True,
    )
    service = ServerManagementService(manager, config_manager=MagicMock())
    added = await service.add_server(
        "only-global",
        "http",
        url="https://global.example.test/mcp",
        enabled=False,
        scope="global",
        project_id=project_id,
    )
    assert added["success"] is True
    global_id = added["id"]
    client = TestClient(_mcp_app(_http_server_for(manager)))

    patched = client.patch(
        "/api/mcp/servers/only-global",
        json={"description": "nope", "project_id": project_id},
    )
    deleted = client.delete(
        "/api/mcp/servers/only-global",
        params={"project_id": project_id},
    )
    enabled = client.patch(
        "/api/mcp/servers/only-global",
        json={"enabled": True, "project_id": project_id},
    )
    for response in (patched, deleted, enabled):
        assert response.status_code == 404
        assert storage.get_server_by_id(global_id) is not None

    ok = client.patch(
        "/api/mcp/servers/only-global",
        json={"description": "global-ok", "scope": "global"},
    )
    assert ok.status_code == 200
    row = storage.get_server("only-global", GLOBAL_PROJECT_ID)
    assert row is not None
    assert row.description == "global-ok"


def test_refresh_preserves_schema_hash_and_embedding_pipeline() -> None:
    config = MCPServerConfig(
        name="github",
        project_id=PROJECT_ID,
        url="https://project.example.test",
        id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        enabled=True,
    )
    manager = MagicMock()
    manager.server_configs = [config]
    manager.refresh_server = AsyncMock()
    manager.get_server_config.return_value = config
    tool = MagicMock()
    tool.name = "list_repos"
    tool.description = "List repos"
    tool.input_schema = {"type": "object"}
    session = AsyncMock()
    session.list_tools.return_value = MagicMock(tools=[tool])
    manager.ensure_connected = AsyncMock(return_value=session)
    db = MagicMock()
    db.db = MagicMock()
    db.get_server.return_value = MagicMock(id=config.id, name="github", project_id=PROJECT_ID)
    db.get_cached_tools.return_value = []
    manager.mcp_db_manager = db
    semantic = MagicMock()
    semantic.embed_tool = AsyncMock()
    server = _http_server_for(manager)
    server._mcp_db_manager = db
    server._tools_handler = MagicMock()
    server._tools_handler._semantic_search = semantic
    client = TestClient(_mcp_app(server))

    with (
        patch("gobby.mcp_proxy.schema_hash.SchemaHashManager") as mock_hash_cls,
        patch("gobby.mcp_proxy.schema_hash.compute_schema_hash", return_value="h1"),
    ):
        hashes = mock_hash_cls.return_value
        hashes.check_tools_for_changes.return_value = {
            "new": ["list_repos"],
            "changed": [],
            "unchanged": [],
        }
        hashes.cleanup_stale_hashes.return_value = 1
        response = client.post(
            "/api/mcp/refresh",
            json={"server": "github", "project_id": PROJECT_ID, "force": True},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    manager.refresh_server.assert_awaited()
    assert config.id in [call.args[0] for call in manager.refresh_server.await_args_list]
    stats = body["stats"]
    by_server = stats["by_server"]
    entry = by_server.get(config.id) or by_server.get("github")
    assert entry is not None
    hashes.store_hash.assert_called()
    hashes.cleanup_stale_hashes.assert_called()
    semantic.embed_tool.assert_awaited()


def test_refresh_embeddings_carry_scoped_server_identity() -> None:
    config = MCPServerConfig(
        name="github",
        project_id=PROJECT_ID,
        url="https://project.example.test",
        id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        enabled=True,
    )
    manager = MagicMock()
    manager.server_configs = [config]
    manager.refresh_server = AsyncMock()
    tool = MagicMock()
    tool.name = "list_repos"
    tool.description = "List repos"
    tool.input_schema = {"type": "object"}
    session = AsyncMock()
    session.list_tools.return_value = MagicMock(tools=[tool])
    manager.ensure_connected = AsyncMock(return_value=session)
    db = MagicMock()
    db.db = MagicMock()
    stored_tool = MagicMock()
    stored_tool.name = "list_repos"
    stored_tool.id = "tool-1"
    db.get_server.return_value = MagicMock(id=config.id)
    db.get_cached_tools.return_value = [stored_tool]
    manager.mcp_db_manager = db
    captured: dict[str, Any] = {}

    async def embed_tool(**kwargs: Any) -> None:
        captured.update(kwargs)

    semantic = MagicMock()
    semantic.embed_tool = AsyncMock(side_effect=embed_tool)
    server = _http_server_for(manager)
    server._mcp_db_manager = db
    server._tools_handler = MagicMock()
    server._tools_handler._semantic_search = semantic
    client = TestClient(_mcp_app(server))

    with (
        patch("gobby.mcp_proxy.schema_hash.SchemaHashManager") as mock_hash_cls,
        patch("gobby.mcp_proxy.schema_hash.compute_schema_hash", return_value="h1"),
    ):
        mock_hash_cls.return_value.check_tools_for_changes.return_value = {
            "new": ["list_repos"],
            "changed": [],
            "unchanged": [],
        }
        mock_hash_cls.return_value.cleanup_stale_hashes.return_value = 0
        response = client.post(
            "/api/mcp/refresh",
            json={"server": "github", "project_id": PROJECT_ID},
        )

    assert response.status_code == 200
    assert captured.get("server_id") == config.id
    assert captured.get("server_name") == "github"
    assert captured.get("project_id") == PROJECT_ID


@pytest.mark.asyncio
async def test_concurrent_patches_and_delete_serialize_under_per_id_lock(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project_id = sample_project["id"]
    manager, added = await _add_templated_instance(temp_db, project_id)
    storage = _storage(manager)
    server_id = added["id"]
    app = _mcp_app(_http_server_for(manager))
    transport = ASGITransport(app=app)
    orig_patch = server_registry_mod._update_server_patch
    orig_remove = server_registry_mod.remove_server

    async def _race(count: int, left: Any, right: Any) -> tuple[Any, Any]:
        barrier = asyncio.Barrier(count)

        async def gated_patch(
            mgr: Any,
            sid: str,
            patch: Any,
            project_id: str | None = None,
        ) -> dict[str, Any]:
            await barrier.wait()
            return await orig_patch(mgr, sid, patch, project_id)

        async def gated_remove(mgr: Any, sid: str, project_id: str | None = None) -> dict[str, Any]:
            await barrier.wait()
            return await orig_remove(mgr, sid, project_id)

        with (
            patch.object(server_registry_mod, "_update_server_patch", gated_patch),
            patch.object(server_registry_mod, "remove_server", gated_remove),
        ):
            return await asyncio.gather(left, right)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first, second = await _race(
            2,
            client.patch(
                "/api/mcp/servers/demo-instance",
                json={"values": {"region": "eu"}, "project_id": project_id},
            ),
            client.patch(
                "/api/mcp/servers/demo-instance",
                json={"values": {"mode": "slow"}, "project_id": project_id},
            ),
        )
        assert first.status_code == 200
        assert second.status_code == 200
        row = storage.get_server_by_id(server_id)
        assert row is not None
        assert _template_values(row)["region"] == "eu"
        assert _template_values(row)["mode"] == "slow"

        null_resp, other_resp = await _race(
            2,
            client.patch(
                "/api/mcp/servers/demo-instance",
                json={"values": {"tag": None}, "project_id": project_id},
            ),
            client.patch(
                "/api/mcp/servers/demo-instance",
                json={"values": {"region": "ap"}, "project_id": project_id},
            ),
        )
        assert null_resp.status_code == 200
        assert other_resp.status_code == 200
        row = storage.get_server_by_id(server_id)
        assert row is not None
        assert "tag" not in _template_values(row)
        assert _template_values(row)["region"] == "ap"
        assert _template_values(row)["mode"] == "slow"

        patch_task, delete_task = await _race(
            2,
            client.patch(
                "/api/mcp/servers/demo-instance",
                json={"values": {"mode": "fast"}, "project_id": project_id},
            ),
            client.delete(
                "/api/mcp/servers/demo-instance",
                params={"project_id": project_id},
            ),
        )
        assert delete_task.status_code in {200, 404}
        assert patch_task.status_code in {200, 404}
        if patch_task.status_code == 404:
            assert _detail(patch_task).get("success") is False
        assert storage.get_server("demo-instance", project_id) is None
        assert storage.get_server_by_id(server_id) is None
