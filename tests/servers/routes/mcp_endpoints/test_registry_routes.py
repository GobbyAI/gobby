"""Tests for MCP registry endpoints (embed, status, refresh)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.dependencies import get_server
from gobby.servers.routes.mcp.tools import create_mcp_router

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

        app.dependency_overrides[get_server] = override_server
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
        mock_server.resolve_project_id.side_effect = ValueError("No project")

        response = client.post("/api/mcp/refresh", json={"cwd": "/tmp"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "No project" in data["error"]

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
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.server_configs = [ext_config]

        # Mock MCP tool
        mock_tool = MagicMock()
        mock_tool.name = "list_repos"
        mock_tool.description = "List GitHub repos"
        mock_tool.inputSchema = {"type": "object", "properties": {"org": {"type": "string"}}}
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
        mock_tool.inputSchema = mock_input_schema

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
        mock_tool.inputSchema = {"type": "object", "properties": {}}

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
