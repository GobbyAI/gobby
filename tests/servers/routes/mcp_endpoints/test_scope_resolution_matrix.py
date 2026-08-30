"""HTTP scope-resolution matrix for MCP execution and refresh (plan 4.3)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.servers.routes.dependencies import get_metrics_manager, get_server
from gobby.servers.routes.mcp.tools import create_mcp_router
from gobby.storage.projects import GLOBAL_PROJECT_ID
from tests.mcp_proxy.services.test_scope_resolution_matrix import (
    FOREIGN_SERVER_ID,
    GLOBAL_SERVER_ID,
    PROJECT_ID,
    PROJECT_SERVER_ID,
    RecordingManager,
    as_mcp,
    scoped_github_configs,
)

pytestmark = pytest.mark.unit


def _client(server: Any) -> TestClient:
    app = FastAPI()
    app.include_router(create_mcp_router())

    async def override_server() -> Any:
        return server

    app.dependency_overrides[get_server] = override_server
    app.dependency_overrides[get_metrics_manager] = lambda: None
    return TestClient(app)


def test_http_scope_resolution_matrix() -> None:
    manager = RecordingManager(scoped_github_configs(), project_id=PROJECT_ID)
    proxy = MagicMock()
    proxy.call_tool = AsyncMock(return_value={"success": True, "result": {"id": PROJECT_SERVER_ID}})
    proxy.get_tool_schema = AsyncMock(
        return_value={
            "success": True,
            "tool": {"name": "ping", "inputSchema": {"type": "object"}},
        }
    )
    server = MagicMock()
    server.mcp_manager = as_mcp(manager)
    server.tool_proxy = proxy
    server._internal_manager = None
    server._tools_handler = None
    server._mcp_db_manager = MagicMock()
    server._mcp_db_manager.db = MagicMock()
    server._mcp_db_manager.db.fetchone.return_value = {"id": 1}
    server.session_manager = None
    server.services = MagicMock()
    server.services.database = None
    server.resolve_project_id.return_value = PROJECT_ID
    client = _client(server)

    called = client.post(
        "/api/mcp/tools/call",
        json={
            "server_name": "github",
            "tool_name": "ping",
            "arguments": {},
            "project_id": PROJECT_ID,
        },
    )
    assert called.status_code == 200
    assert called.json()["success"] is True
    call_kwargs = proxy.call_tool.await_args.kwargs
    assert call_kwargs["project_id"] == PROJECT_ID

    schema = client.post(
        "/api/mcp/tools/schema",
        json={
            "server_name": "github",
            "tool_name": "ping",
            "project_id": PROJECT_ID,
        },
    )
    assert schema.status_code == 200
    assert schema.json()["success"] is True

    by_id = client.post(
        "/api/mcp/tools/call",
        json={
            "server_id": PROJECT_SERVER_ID,
            "tool_name": "ping",
            "arguments": {},
            "project_id": PROJECT_ID,
        },
    )
    assert by_id.status_code == 200
    assert by_id.json()["success"] is True

    foreign = client.post(
        "/api/mcp/tools/call",
        json={
            "server_id": FOREIGN_SERVER_ID,
            "tool_name": "ping",
            "arguments": {},
            "project_id": PROJECT_ID,
        },
    )
    payload = foreign.json()
    detail = payload.get("detail", payload)
    assert foreign.status_code in {200, 404}
    assert detail.get("success") is False

    session_bound = client.post(
        "/api/mcp/tools/call",
        json={
            "server_name": "github",
            "tool_name": "ping",
            "arguments": {},
            "scope": "global",
            "project_id": PROJECT_ID,
        },
        headers={"X-Gobby-Project-Id": PROJECT_ID},
    )
    assert session_bound.status_code == 200
    global_call = [
        call
        for call in proxy.call_tool.await_args_list
        if call.kwargs.get("project_id") == GLOBAL_PROJECT_ID
    ]
    assert global_call

    refreshed = client.post(
        "/api/mcp/refresh",
        json={"server": "github", "project_id": PROJECT_ID, "force": False},
    )
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["success"] is True
    refreshed_ids = manager.method_ids("refresh_server")
    assert PROJECT_SERVER_ID in refreshed_ids
    assert GLOBAL_SERVER_ID not in refreshed_ids
    assert FOREIGN_SERVER_ID not in refreshed_ids
    stats = body["stats"]["by_server"]
    assert PROJECT_SERVER_ID in stats or "github" in stats
    per_instance = next(iter(stats.values()))
    assert "name" in per_instance or PROJECT_SERVER_ID in stats
    _ = cast(MCPClientManager, manager)
