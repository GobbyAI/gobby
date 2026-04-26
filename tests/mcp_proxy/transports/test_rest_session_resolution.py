"""REST MCP endpoint coverage for nested session_id reference resolution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.servers.routes.mcp.endpoints.execution import call_mcp_tool

pytestmark = pytest.mark.unit


SESSION_UUID_3 = "33333333-3333-4333-8333-333333333333"


def _make_session_manager() -> MagicMock:
    session_manager = MagicMock()
    session_manager.db = None
    session_manager.resolve_session_reference.return_value = SESSION_UUID_3
    session_manager.get.return_value = SimpleNamespace(
        external_id="ext-session-3",
        project_id="proj-1",
    )
    return session_manager


def _make_server() -> MagicMock:
    server = MagicMock()
    server.session_manager = _make_session_manager()
    server.tool_proxy = MagicMock()
    server.tool_proxy.call_tool = AsyncMock(return_value={"success": True, "ok": True})
    server._internal_manager = None
    server.mcp_manager = None
    return server


def _make_request() -> MagicMock:
    request = MagicMock()
    request.headers = {"x-gobby-project-id": "proj-1"}
    request.json = AsyncMock(
        return_value={
            "server_name": "gobby-sessions",
            "tool_name": "get_session",
            "arguments": {"session_id": "#3"},
        }
    )
    return request


@pytest.mark.asyncio
async def test_rest_call_resolves_arguments_session_id_before_dispatch() -> None:
    server = _make_server()
    request = _make_request()

    await call_mcp_tool(request, server)

    server.session_manager.resolve_session_reference.assert_called_once_with("#3", "proj-1")
    server.tool_proxy.call_tool.assert_awaited_once_with(
        "gobby-sessions",
        "get_session",
        {"session_id": SESSION_UUID_3},
        session_id=SESSION_UUID_3,
    )
