"""Tests for MCP discovery endpoints (list tools, recommend, search)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.dependencies import get_metrics_manager, get_server
from gobby.servers.routes.mcp.tools import create_mcp_router

pytestmark = pytest.mark.unit


class TestMCPDiscoveryRoutes:
    @pytest.fixture
    def mock_server(self) -> MagicMock:
        server = MagicMock()
        server.mcp_manager = None
        server._internal_manager = None
        server._tools_handler = None
        server._mcp_db_manager = None
        server.resolve_project_id.return_value = "proj-1"
        server.metrics_manager = None
        return server

    @pytest.fixture
    def client(self, mock_server: MagicMock) -> TestClient:
        app = FastAPI()
        router = create_mcp_router()
        app.include_router(router)

        async def override_server():
            return mock_server

        async def override_metrics():
            return mock_server.metrics_manager

        app.dependency_overrides[get_server] = override_server
        app.dependency_overrides[get_metrics_manager] = override_metrics
        return TestClient(app)

    # -----------------------------------------------------------------
    # GET /mcp/tools (list_all_mcp_tools)
    # -----------------------------------------------------------------

    def test_list_tools_empty(self, client: TestClient) -> None:
        response = client.get("/api/mcp/tools")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["tools"] == {}
        assert "response_time_ms" in data

    def test_list_tools_internal_servers(self, client: TestClient, mock_server: MagicMock) -> None:
        registry = MagicMock()
        registry.name = "gobby-tasks"
        registry.list_tools.return_value = [
            {"name": "create_task", "brief": "Create a task"},
        ]
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.return_value = [registry]

        response = client.get("/api/mcp/tools")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "gobby-tasks" in data["tools"]
        assert len(data["tools"]["gobby-tasks"]) == 1

    def test_list_tools_with_server_filter_internal(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        registry = MagicMock()
        registry.name = "gobby-tasks"
        registry.list_tools.return_value = [{"name": "t1", "brief": "Tool 1"}]
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.is_internal.return_value = True
        mock_server._internal_manager.get_registry.return_value = registry

        response = client.get("/api/mcp/tools?server_filter=gobby-tasks")
        assert response.status_code == 200
        data = response.json()
        assert "gobby-tasks" in data["tools"]

    def test_list_tools_with_server_filter_external_disabled(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.is_internal.return_value = False
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.has_server.return_value = True
        config = MagicMock()
        config.enabled = False
        mock_server.mcp_manager._configs = {"ext-server": config}

        response = client.get("/api/mcp/tools?server_filter=ext-server")
        assert response.status_code == 200
        data = response.json()
        assert data["tools"]["ext-server"] == []

    def test_list_tools_with_server_filter_external_connected(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """External server with filter, enabled, successfully connected."""
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.is_internal.return_value = False
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.has_server.return_value = True
        config = MagicMock()
        config.enabled = True
        mock_server.mcp_manager._configs = {"ext-server": config}

        # Mock MCP session
        mock_tool = MagicMock()
        mock_tool.name = "ext_tool"
        mock_tool.description = "External tool description that is long enough"
        mock_session = AsyncMock()
        mock_tools_result = MagicMock()
        mock_tools_result.tools = [mock_tool]
        mock_session.list_tools.return_value = mock_tools_result
        mock_server.mcp_manager.ensure_connected = AsyncMock(return_value=mock_session)

        response = client.get("/api/mcp/tools?server_filter=ext-server")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        tools = data["tools"]["ext-server"]
        assert len(tools) == 1
        assert tools[0]["name"] == "ext_tool"

    def test_list_tools_with_server_filter_external_connection_error(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """External server with filter, ensure_connected raises."""
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.is_internal.return_value = False
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.has_server.return_value = True
        config = MagicMock()
        config.enabled = True
        mock_server.mcp_manager._configs = {"ext-server": config}
        mock_server.mcp_manager.ensure_connected = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )

        response = client.get("/api/mcp/tools?server_filter=ext-server")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["tools"]["ext-server"] == []

    def test_list_tools_with_server_filter_external_no_config(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """External server with filter, config not found (None)."""
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.is_internal.return_value = False
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.has_server.return_value = True
        mock_server.mcp_manager._configs = {}  # No config for this server

        response = client.get("/api/mcp/tools?server_filter=ext-server")
        assert response.status_code == 200
        data = response.json()
        # No config means config is None -> enabled check not hit -> falls into ensure_connected
        # Actually: server_config = _configs.get("ext-server") -> None, so None -> skip
        assert data["success"] is True

    def test_list_tools_all_with_external_servers(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """List all tools including external MCP servers."""
        # Internal
        registry = MagicMock()
        registry.name = "gobby-tasks"
        registry.list_tools.return_value = [{"name": "t1", "brief": "Tool 1"}]
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.return_value = [registry]

        # External
        ext_config = MagicMock()
        ext_config.name = "github-mcp"
        ext_config.enabled = True
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.server_configs = [ext_config]

        mock_tool = MagicMock()
        mock_tool.name = "list_repos"
        mock_tool.description = "List GitHub repos"
        mock_session = AsyncMock()
        mock_tools_result = MagicMock()
        mock_tools_result.tools = [mock_tool]
        mock_session.list_tools.return_value = mock_tools_result
        mock_server.mcp_manager.ensure_connected = AsyncMock(return_value=mock_session)

        response = client.get("/api/mcp/tools")
        assert response.status_code == 200
        data = response.json()
        assert "gobby-tasks" in data["tools"]
        assert "github-mcp" in data["tools"]
        assert data["tools"]["github-mcp"][0]["name"] == "list_repos"

    def test_list_tools_external_server_error_all(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """External server error in all-servers mode returns empty list for that server."""
        ext_config = MagicMock()
        ext_config.name = "broken-server"
        ext_config.enabled = True
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.server_configs = [ext_config]
        mock_server.mcp_manager.ensure_connected = AsyncMock(
            side_effect=ConnectionError("unreachable")
        )

        response = client.get("/api/mcp/tools")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["tools"]["broken-server"] == []

    def test_list_tools_external_disabled_skipped(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """Disabled external servers are skipped in all-servers mode."""
        ext_config = MagicMock()
        ext_config.name = "disabled-server"
        ext_config.enabled = False
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.server_configs = [ext_config]

        response = client.get("/api/mcp/tools")
        assert response.status_code == 200
        data = response.json()
        assert "disabled-server" not in data["tools"]

    def test_list_tools_tool_without_description(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """External tool with no description uses empty string."""
        ext_config = MagicMock()
        ext_config.name = "server-a"
        ext_config.enabled = True
        mock_server.mcp_manager = MagicMock()
        mock_server.mcp_manager.server_configs = [ext_config]

        mock_tool = MagicMock(spec=[])  # No description attribute
        mock_tool.name = "no_desc_tool"
        mock_session = AsyncMock()
        mock_tools_result = MagicMock()
        mock_tools_result.tools = [mock_tool]
        mock_session.list_tools.return_value = mock_tools_result
        mock_server.mcp_manager.ensure_connected = AsyncMock(return_value=mock_session)

        response = client.get("/api/mcp/tools")
        assert response.status_code == 200
        data = response.json()
        tool = data["tools"]["server-a"][0]
        assert tool["brief"] == ""

    def test_list_tools_with_metrics(self, client: TestClient, mock_server: MagicMock) -> None:
        """Include metrics enrichment when include_metrics=true."""
        registry = MagicMock()
        registry.name = "gobby-tasks"
        registry.list_tools.return_value = [
            {"name": "create_task", "brief": "Create a task"},
            {"name": "list_tasks", "brief": "List tasks"},
        ]
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.return_value = [registry]

        # Set up metrics manager
        metrics_mgr = MagicMock()
        metrics_mgr.get_metrics.return_value = {
            "tools": [
                {
                    "server_name": "gobby-tasks",
                    "tool_name": "create_task",
                    "call_count": 42,
                    "success_rate": 0.95,
                    "avg_latency_ms": 123.4,
                },
            ]
        }
        mock_server.metrics_manager = metrics_mgr

        response = client.get("/api/mcp/tools?include_metrics=true")
        assert response.status_code == 200
        data = response.json()
        tools = data["tools"]["gobby-tasks"]

        # create_task has metrics
        create_task = next(t for t in tools if t["name"] == "create_task")
        assert create_task["call_count"] == 42
        assert create_task["success_rate"] == 0.95
        assert create_task["avg_latency_ms"] == 123.4

        # list_tasks has no metrics (defaults)
        list_tasks = next(t for t in tools if t["name"] == "list_tasks")
        assert list_tasks["call_count"] == 0
        assert list_tasks["success_rate"] is None
        assert list_tasks["avg_latency_ms"] is None

    def test_list_tools_with_metrics_project_resolve_fails(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """When project resolve fails for metrics, metrics enrichment is skipped."""
        mock_server.resolve_project_id.side_effect = ValueError("No project")
        registry = MagicMock()
        registry.name = "gobby-tasks"
        registry.list_tools.return_value = [{"name": "t1", "brief": "Tool"}]
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.return_value = [registry]

        response = client.get("/api/mcp/tools?include_metrics=true")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        # No metrics enrichment should have happened
        tool = data["tools"]["gobby-tasks"][0]
        assert "call_count" not in tool

    def test_list_tools_metrics_with_non_dict_tool(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """Metrics enrichment guards against non-dict entries in tool list."""
        registry = MagicMock()
        registry.name = "gobby-tasks"
        # Simulate a non-dict entry and a missing-name dict
        registry.list_tools.return_value = [
            "not-a-dict",
            {"no_name_key": "value"},
            {"name": "real_tool", "brief": "Real"},
        ]
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.return_value = [registry]

        metrics_mgr = MagicMock()
        metrics_mgr.get_metrics.return_value = {"tools": []}
        mock_server.metrics_manager = metrics_mgr

        response = client.get("/api/mcp/tools?include_metrics=true")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_list_tools_error(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server._internal_manager = MagicMock()
        mock_server._internal_manager.get_all_registries.side_effect = RuntimeError("boom")

        response = client.get("/api/mcp/tools")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "boom" in data["error"]

    # -----------------------------------------------------------------
    # POST /mcp/tools/recommend (recommend_mcp_tools)
    # -----------------------------------------------------------------

    def test_recommend_no_task_description(self, client: TestClient) -> None:
        response = client.post("/api/mcp/tools/recommend", json={"top_k": 5})
        assert response.status_code == 400

    def test_recommend_no_tools_handler(self, client: TestClient) -> None:
        response = client.post(
            "/api/mcp/tools/recommend",
            json={"task_description": "create a file"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not initialized" in data["error"]

    def test_recommend_with_tools_handler(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server._tools_handler = MagicMock()
        mock_server._tools_handler.recommend_tools = AsyncMock(
            return_value={
                "success": True,
                "recommendations": [{"tool": "write_file", "score": 0.9}],
            }
        )

        response = client.post(
            "/api/mcp/tools/recommend",
            json={"task_description": "create a file"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["recommendations"]) == 1
        assert "response_time_ms" in data

    def test_recommend_with_all_params(self, client: TestClient, mock_server: MagicMock) -> None:
        """Pass all optional params to recommend."""
        mock_server._tools_handler = MagicMock()
        mock_server._tools_handler.recommend_tools = AsyncMock(
            return_value={"success": True, "recommendations": []}
        )

        response = client.post(
            "/api/mcp/tools/recommend",
            json={
                "task_description": "query db",
                "agent_id": "agent-1",
                "search_mode": "hybrid",
                "top_k": 5,
                "min_similarity": 0.5,
                "cwd": "/projects/test",
            },
        )
        assert response.status_code == 200
        # Verify the params were passed through
        call_kwargs = mock_server._tools_handler.recommend_tools.call_args[1]
        assert call_kwargs["task_description"] == "query db"
        assert call_kwargs["agent_id"] == "agent-1"
        assert call_kwargs["search_mode"] == "hybrid"
        assert call_kwargs["top_k"] == 5

    def test_recommend_malformed_json(self, client: TestClient) -> None:
        response = client.post(
            "/api/mcp/tools/recommend",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Malformed JSON" in data["error"]

    def test_recommend_semantic_project_resolve_fail(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        mock_server.resolve_project_id.side_effect = ValueError("No project")

        response = client.post(
            "/api/mcp/tools/recommend",
            json={
                "task_description": "query db",
                "search_mode": "semantic",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "No project" in data["error"]
        assert data["task"] == "query db"

    def test_recommend_hybrid_project_resolve_fail(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """Hybrid mode also triggers project resolution."""
        mock_server.resolve_project_id.side_effect = ValueError("No project found")

        response = client.post(
            "/api/mcp/tools/recommend",
            json={
                "task_description": "analyze data",
                "search_mode": "hybrid",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False

    def test_recommend_general_exception(self, client: TestClient, mock_server: MagicMock) -> None:
        """Uncaught exception returns error response."""
        mock_server._tools_handler = MagicMock()
        mock_server._tools_handler.recommend_tools = AsyncMock(
            side_effect=RuntimeError("unexpected error")
        )

        response = client.post(
            "/api/mcp/tools/recommend",
            json={"task_description": "cause an error"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "unexpected error" in data["error"]

    # -----------------------------------------------------------------
    # POST /mcp/tools/search (search_mcp_tools)
    # -----------------------------------------------------------------

    def test_search_no_query(self, client: TestClient) -> None:
        response = client.post("/api/mcp/tools/search", json={"top_k": 5})
        assert response.status_code == 400

    def test_search_no_semantic_search(self, client: TestClient) -> None:
        response = client.post(
            "/api/mcp/tools/search",
            json={"query": "create file"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not configured" in data["error"]

    def test_search_project_resolve_fail(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server.resolve_project_id.side_effect = ValueError("No project")

        response = client.post(
            "/api/mcp/tools/search",
            json={"query": "create file"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["query"] == "create file"

    def test_search_malformed_json(self, client: TestClient) -> None:
        response = client.post(
            "/api/mcp/tools/search",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Malformed JSON" in data["error"]

    def test_search_with_results(self, client: TestClient, mock_server: MagicMock) -> None:
        result_mock = MagicMock()
        result_mock.to_dict.return_value = {"tool": "write_file", "score": 0.85}

        semantic_search = MagicMock()
        semantic_search.get_embeddings_for_project.return_value = ["emb1"]
        semantic_search.search_tools = AsyncMock(return_value=[result_mock])

        mock_server._tools_handler = MagicMock()
        mock_server._tools_handler._semantic_search = semantic_search

        response = client.post(
            "/api/mcp/tools/search",
            json={"query": "create file"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["total_results"] == 1
        assert data["results"][0]["tool"] == "write_file"

    def test_search_with_all_params(self, client: TestClient, mock_server: MagicMock) -> None:
        """Pass all optional params to search."""
        semantic_search = MagicMock()
        semantic_search.get_embeddings_for_project.return_value = ["emb1"]
        semantic_search.search_tools = AsyncMock(return_value=[])
        mock_server._tools_handler = MagicMock()
        mock_server._tools_handler._semantic_search = semantic_search

        response = client.post(
            "/api/mcp/tools/search",
            json={
                "query": "list files",
                "top_k": 5,
                "min_similarity": 0.5,
                "server": "gobby-tasks",
                "cwd": "/projects/test",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        # Verify params passed through
        call_kwargs = semantic_search.search_tools.call_args[1]
        assert call_kwargs["query"] == "list files"
        assert call_kwargs["top_k"] == 5
        assert call_kwargs["min_similarity"] == 0.5
        assert call_kwargs["server_filter"] == "gobby-tasks"

    def test_search_triggers_embedding_generation(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        semantic_search = MagicMock()
        semantic_search.get_embeddings_for_project.return_value = []  # No embeddings

        mock_server._tools_handler = MagicMock()
        mock_server._tools_handler._semantic_search = semantic_search
        mock_server._mcp_db_manager = MagicMock()

        response = client.post(
            "/api/mcp/tools/search",
            json={"query": "create file"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["embeddings_generating"] is True
        assert data["total_results"] == 0
        assert "retry" in data["message"].lower()

    def test_search_no_embeddings_no_mcp_db_manager(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """No embeddings but no _mcp_db_manager - should proceed to search (with empty embeddings)."""
        semantic_search = MagicMock()
        semantic_search.get_embeddings_for_project.return_value = []
        semantic_search.search_tools = AsyncMock(return_value=[])
        mock_server._tools_handler = MagicMock()
        mock_server._tools_handler._semantic_search = semantic_search
        mock_server._mcp_db_manager = None  # No db manager

        response = client.post(
            "/api/mcp/tools/search",
            json={"query": "create file"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        # Should fall through to search_tools since no _mcp_db_manager
        assert data["total_results"] == 0

    def test_search_semantic_error(self, client: TestClient, mock_server: MagicMock) -> None:
        semantic_search = MagicMock()
        semantic_search.get_embeddings_for_project.side_effect = RuntimeError("embed fail")

        mock_server._tools_handler = MagicMock()
        mock_server._tools_handler._semantic_search = semantic_search

        response = client.post(
            "/api/mcp/tools/search",
            json={"query": "create file"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "embed fail" in data["error"]

    def test_search_general_exception(self, client: TestClient, mock_server: MagicMock) -> None:
        """Uncaught outer exception handler."""
        # Make resolve_project_id raise a non-ValueError
        mock_server.resolve_project_id.side_effect = RuntimeError("catastrophic failure")

        response = client.post(
            "/api/mcp/tools/search",
            json={"query": "trigger error"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "catastrophic failure" in data["error"]

    def test_search_tools_handler_no_semantic(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """tools_handler exists but _semantic_search is None."""
        mock_server._tools_handler = MagicMock()
        mock_server._tools_handler._semantic_search = None

        response = client.post(
            "/api/mcp/tools/search",
            json={"query": "create file"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not configured" in data["error"]
