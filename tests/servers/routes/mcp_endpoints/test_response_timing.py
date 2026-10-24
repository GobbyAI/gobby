"""MCP route bodies carry no per-call timing; response_time_ms is gone from the wire."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.storage.sessions import SessionManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

_TIMING_KEY = "response_time_ms"


class _InternalRegistry:
    name = "gobby-tasks"

    def list_tools(self) -> list[dict[str, Any]]:
        return [{"name": "list_tasks", "description": "List tasks"}]

    def get_schema(self, tool_name: str) -> dict[str, Any] | None:
        return {"type": "object", "properties": {}} if tool_name == "list_tasks" else None

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "tasks": []}


@pytest.fixture
def server(session_storage: SessionManager) -> Iterator[Any]:
    server = create_http_server(port=60887, test_mode=True, session_manager=session_storage)
    registry = _InternalRegistry()
    internal = MagicMock()
    internal.is_internal.side_effect = lambda server_name: server_name == "gobby-tasks"
    internal.get_registry.side_effect = lambda server_name: (
        registry if server_name == "gobby-tasks" else None
    )
    internal.get_all_registries.return_value = [registry]
    server._internal_manager = internal
    mcp_manager = MagicMock()
    mcp_manager.project_id = None
    mcp_manager.session_manager = None
    server._tools_handler = MagicMock(
        tool_proxy=ToolProxyService(
            mcp_manager, internal_manager=internal, validate_arguments=False
        )
    )
    yield server


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param("GET", "/api/mcp/status", None, id="status"),
        pytest.param("GET", "/api/mcp/gobby-tasks/tools", None, id="list-tools"),
        pytest.param(
            "POST",
            "/api/mcp/tools/schema",
            {"server_name": "gobby-tasks", "tool_name": "list_tasks"},
            id="tool-schema",
        ),
        pytest.param(
            "POST",
            "/api/mcp/tools/call",
            {"server_name": "gobby-tasks", "tool_name": "list_tasks", "arguments": {}},
            id="call-success",
        ),
        pytest.param(
            "POST",
            "/api/mcp/tools/call",
            {"server_name": "gobby-tasks", "tool_name": "missing_tool", "arguments": {}},
            id="call-unknown-tool",
        ),
        pytest.param("POST", "/api/mcp/gobby-tasks/tools/list_tasks", {}, id="proxy"),
        pytest.param("POST", "/api/mcp/tools/search", {"query": "create a file"}, id="search"),
        pytest.param("POST", "/api/mcp/tools/embed", {}, id="embed"),
        pytest.param("POST", "/api/mcp/refresh", {"force": False}, id="refresh"),
        pytest.param("PATCH", "/api/mcp/servers/test-server", {"enabled": False}, id="set-enabled"),
    ],
)
def test_route_body_has_no_response_time(
    server: Any, method: str, path: str, body: dict[str, Any] | None
) -> None:
    with TestClient(server.app) as client:
        response = client.request(method, path, json=body)

    payload = response.json()
    assert isinstance(payload, dict)
    assert _TIMING_KEY not in json.dumps(payload)
