"""External MCP fallbacks retain the shared Unicode boundary."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult, TextContent

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.servers.routes.mcp.endpoints import execution, request_context
from gobby.servers.websocket.handlers.core import HandlerMixin

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["websocket", "http_call", "http_proxy"])
@pytest.mark.parametrize("model_result", [False, True])
async def test_direct_external_calls_are_cleaned(
    path: str, model_result: bool, caplog: pytest.LogCaptureFixture
) -> None:
    hidden = "private payload\u202e\U000e0061"
    downstream = (
        CallToolResult(content=[TextContent(type="text", text=hidden)])
        if model_result
        else {"text": hidden}
    )
    manager = MCPClientManager()
    server = MagicMock()
    server.mcp_manager = manager
    server.tool_proxy = None
    server._internal_manager = None
    request = MagicMock()
    request.headers = {}
    request.query_params = {}
    request.json = AsyncMock(
        return_value={"server_name": "external", "tool_name": "read", "arguments": {}}
    )
    token = SimpleNamespace(resolved_session_id=None)
    with (
        patch("gobby.mcp_proxy.manager.invocation.call_tool", AsyncMock(return_value=downstream)),
        patch("gobby.mcp_proxy.services.server_resolution.resolved_server_id", return_value="ext"),
        patch.object(execution, "resolve_server", return_value=SimpleNamespace(name="external")),
        patch.object(execution, "_http_request_scope", return_value="GLOBAL"),
        patch.object(execution, "_mcp_call_timeout", return_value=30.0),
        patch.object(execution, "_enforce_workflow_for_request", AsyncMock(return_value=False)),
        patch.object(request_context, "_set_context_for_request", AsyncMock(return_value=token)),
        patch.object(request_context, "_reset_context"),
        caplog.at_level(logging.INFO, logger="gobby.prompts.rendering"),
    ):
        if path == "websocket":
            handler = HandlerMixin()
            handler.mcp_manager = manager
            result = await handler._call_external_mcp("external", "read", {})
        elif path == "http_call":
            result = (await execution.call_mcp_tool(request, server))["result"]
        else:
            result = (await execution.mcp_proxy("external", "read", request, server))["result"]

    payload = result.model_dump(mode="json") if isinstance(result, CallToolResult) else result
    if model_result:
        assert payload["content"][0]["text"] == "private payload"
        assert isinstance(downstream, CallToolResult)
        assert isinstance(downstream.content[0], TextContent)
        assert downstream.content[0].text == hidden
    else:
        assert payload == {"text": "private payload"}
        assert downstream == {"text": hidden}
    assert "bidi=1" in caplog.text
    assert "tags=1" in caplog.text
    assert "private payload" not in caplog.text
