"""HTTP MCP wrapper coverage for tool-result delivery shaping."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.servers.routes.mcp.endpoints.execution import call_mcp_tool, mcp_proxy

pytestmark = pytest.mark.unit

MAX_ENVELOPE_CHARS = 2_000


def _server(result: dict[str, Any]) -> MagicMock:
    server = MagicMock()
    server.session_manager = None
    server.config = None
    server.tool_proxy = MagicMock()
    server.tool_proxy.call_tool = AsyncMock(return_value=result)
    server._internal_manager = None
    server.mcp_manager = None
    return server


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["structured", "server_tool"])
@pytest.mark.parametrize(
    "tool_result",
    [
        {
            "offloaded": True,
            "result_id": "11111111-1111-4111-8111-111111111111",
            "preview": "x" * 1_300,
        },
        {
            "result_id": "11111111-1111-4111-8111-111111111111",
            "content": "x" * 1_300,
            "offset": 0,
            "next_offset": 1_300,
            "total_chars": 4_000,
        },
    ],
)
async def test_http_wrapper_final_result_stays_within_shared_cap(
    route: str,
    tool_result: dict[str, Any],
) -> None:
    server = _server(tool_result)
    request = MagicMock()
    request.headers = {}
    request.query_params = {"intent": "wrapper-summary"}

    if route == "structured":
        request.json = AsyncMock(
            return_value={
                "server_name": "example",
                "tool_name": "large_tool",
                "arguments": {"intent": "target-value"},
                "intent": "wrapper-summary",
            }
        )
        response = await call_mcp_tool(request, server)
    else:
        request.json = AsyncMock(return_value={"intent": "target-value"})
        response = await mcp_proxy("example", "large_tool", request, server)

    assert response["success"] is True
    assert response["result"] == tool_result
    assert len(json.dumps(response, ensure_ascii=False, default=str)) <= MAX_ENVELOPE_CHARS
    call = server.tool_proxy.call_tool.await_args
    assert call.args[2] == {"intent": "target-value"}
    assert call.kwargs["wrapper_originated"] is True
    assert call.kwargs["intent"] == "wrapper-summary"
