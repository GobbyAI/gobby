"""Ask discovery must authorize before contacting a foreign MCP server."""

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from gobby.mcp_proxy.services import server_resolution
from gobby.servers.routes.dependencies import get_metrics_manager, get_server
from gobby.servers.routes.mcp.endpoints import ask_policy, discovery, execution, request_context
from gobby.servers.routes.mcp.tools import create_mcp_router

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.servers.http import HTTPServer


def _request(body: dict[str, object]) -> MagicMock:
    request = MagicMock()
    request.headers = {}
    request.query_params = {}
    request.json = AsyncMock(return_value=body)
    return request


def _context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        request_context,
        "_set_context_for_request",
        AsyncMock(return_value=SimpleNamespace(resolved_session_id=None)),
    )
    monkeypatch.setattr(request_context, "_reset_context", Mock())
    monkeypatch.setattr(execution, "_http_request_scope", lambda *_args: "project")


def _client(server: "HTTPServer") -> TestClient:
    app = FastAPI()
    app.include_router(create_mcp_router())

    async def override_server() -> "HTTPServer":
        return server

    app.dependency_overrides[get_server] = override_server
    app.dependency_overrides[get_metrics_manager] = lambda: None
    return TestClient(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["named", "aggregate"])
@pytest.mark.parametrize("restricted", [True, False])
async def test_discovery_authorizes_before_foreign_server_side_effects(
    endpoint: str, restricted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = Request({"type": "http", "headers": []})
    remote = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])))
    manager = SimpleNamespace(ensure_connected=AsyncMock(return_value=remote))
    server = cast(
        "HTTPServer",
        SimpleNamespace(tool_proxy=None, mcp_manager=manager, _internal_manager=None),
    )
    resolve = Mock(return_value=SimpleNamespace(id="foreign-id", name="foreign", enabled=True))
    monkeypatch.setattr(server_resolution, "resolve_server", resolve)
    monkeypatch.setattr(execution, "resolve_server", resolve)
    monkeypatch.setattr(request_context, "request_mcp_scope", lambda *_args: "project")
    monkeypatch.setattr(execution, "_http_request_scope", lambda *_args: "project")
    monkeypatch.setattr(
        request_context,
        "_set_context_for_request",
        AsyncMock(return_value=SimpleNamespace(resolved_session_id="caller")),
    )
    monkeypatch.setattr(request_context, "_reset_context", Mock())
    allowed = frozenset({("gobby-ask", "query_evidence")}) if restricted else None
    monkeypatch.setattr(ask_policy, "current_ask_allowed_tools", lambda _service: allowed)
    monkeypatch.setattr(discovery, "_current_ask_tools", AsyncMock(return_value=allowed))
    live_discovery = AsyncMock(return_value=[])
    monkeypatch.setattr(discovery, "_list_external_server_tools", live_discovery)

    if endpoint == "named":
        result = await execution.list_mcp_tools(
            "foreign",
            request,
            server=server,
            internal_manager=None,
            mcp_manager=cast("MCPClientManager", manager),
        )
    else:
        result = await discovery.list_all_mcp_tools(
            request, server_filter="foreign", server=server, metrics_manager=None
        )

    assert result["success"] is True
    if restricted:
        resolve.assert_not_called()
        manager.ensure_connected.assert_not_awaited()
        remote.list_tools.assert_not_awaited()
        live_discovery.assert_not_awaited()
        assert not result["tools"]
    else:
        resolve.assert_called_once()
        if endpoint == "named":
            manager.ensure_connected.assert_awaited_once_with("foreign-id")
            remote.list_tools.assert_awaited_once()
        else:
            live_discovery.assert_awaited_once()


@pytest.mark.asyncio
async def test_ordinary_http_discovery_hides_internal_stage_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _context(monkeypatch)
    registry = SimpleNamespace(
        list_tools=lambda: [
            {"name": "start_ask_run"},
            {"name": "seed"},
            {"name": "query_evidence"},
        ]
    )
    internal = MagicMock()
    internal.is_internal.return_value = True
    internal.get_registry.return_value = registry
    server = cast(
        "HTTPServer",
        SimpleNamespace(tool_proxy=None, mcp_manager=None, _internal_manager=internal),
    )

    result = await execution.list_mcp_tools(
        "gobby-ask",
        _request({}),
        server=server,
        internal_manager=internal,
        mcp_manager=None,
    )

    assert result["success"] is True
    assert [tool["name"] for tool in result["tools"]] == [
        "start_ask_run",
        "query_evidence",
    ]


@pytest.mark.asyncio
async def test_http_schema_alias_hides_internal_stage_before_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _context(monkeypatch)
    internal = MagicMock()
    internal.is_internal.return_value = False
    proxy = MagicMock()
    proxy.find_tool_server.return_value = "gobby-ask"
    proxy.get_tool_schema = AsyncMock()
    server = cast(
        "HTTPServer",
        SimpleNamespace(
            tool_proxy=proxy,
            mcp_manager=MagicMock(),
            _internal_manager=internal,
        ),
    )
    monkeypatch.setattr(execution, "resolve_server", Mock(return_value=None))

    result = await execution.get_tool_schema(
        _request({"server_name": "gobby", "tool_name": "seed"}),
        server,
    )

    assert result["success"] is False
    assert result["error_code"] == "TOOL_BLOCKED"
    assert result["server_name"] == "gobby-ask"
    proxy.get_tool_schema.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ["aggregate", "named"])
@pytest.mark.parametrize("server_name", ["gobby-ask", "gobby"])
def test_http_no_proxy_fallback_denies_internal_stage_before_registry(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    server_name: str,
) -> None:
    _context(monkeypatch)
    internal = MagicMock()
    internal.is_internal.return_value = True
    internal.find_tool_server.return_value = "gobby-ask"
    server = cast(
        "HTTPServer",
        SimpleNamespace(tool_proxy=None, mcp_manager=None, _internal_manager=internal),
    )
    client = _client(server)
    arguments = {"run_id": "target", "project_id": "project"}

    if endpoint == "aggregate":
        response = client.post(
            "/api/mcp/tools/call",
            json={
                "server_name": server_name,
                "tool_name": "seed",
                "arguments": arguments,
            },
        )
    else:
        response = client.post(
            f"/api/mcp/{server_name}/tools/seed",
            json=arguments,
        )

    assert response.status_code == 200
    result = response.json()
    assert result["success"] is False
    assert result["error_code"] == "TOOL_BLOCKED"
    assert result["server_name"] == "gobby-ask"
    assert "owning Ask pipeline" in result["error"]
    internal.get_registry.assert_not_called()
