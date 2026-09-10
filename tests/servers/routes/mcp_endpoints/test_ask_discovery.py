"""Ask discovery must authorize before contacting a foreign MCP server."""

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.requests import Request

from gobby.mcp_proxy.services import server_resolution
from gobby.servers.routes.mcp.endpoints import discovery, execution, request_context

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.servers.http import HTTPServer


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
    monkeypatch.setattr(execution, "current_ask_allowed_tools", lambda _service: allowed)
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
