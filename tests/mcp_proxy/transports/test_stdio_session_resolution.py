"""Stdio-to-REST MCP endpoint coverage for nested session_id resolution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.servers.routes.mcp.endpoints.execution import mcp_proxy

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


def _make_server() -> tuple[MagicMock, MagicMock]:
    session_manager = _make_session_manager()
    mcp_manager = MagicMock()
    mcp_manager.project_id = None
    mcp_manager.call_tool = AsyncMock(return_value={"success": True, "ok": True})

    internal_manager = MagicMock()
    internal_manager.is_internal.return_value = False

    hook_manager = SimpleNamespace(
        _session_manager=session_manager,
        handle=MagicMock(),
    )

    server = MagicMock()
    server.session_manager = session_manager
    server.tool_proxy = ToolProxyService(
        mcp_manager=mcp_manager,
        internal_manager=internal_manager,
        validate_arguments=False,
        hook_manager_resolver=lambda: hook_manager,
    )
    server._internal_manager = None
    server.mcp_manager = None
    return server, mcp_manager


def _make_stdio_request() -> MagicMock:
    request = MagicMock()
    request.headers = {
        "x-gobby-project-id": "proj-1",
        "x-gobby-session-id": "wrapper-session",
    }
    request.json = AsyncMock(return_value={"session_id": "#3"})
    return request


@pytest.mark.asyncio
async def test_stdio_rest_path_resolves_arguments_session_id_before_dispatch() -> None:
    server, mcp_manager = _make_server()
    request = _make_stdio_request()

    await mcp_proxy("gobby-sessions", "get_session", request, server)

    server.session_manager.resolve_session_reference.assert_any_call("#3", "proj-1")
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-sessions",
        "get_session",
        {"session_id": SESSION_UUID_3},
        session_id=SESSION_UUID_3,
    )
