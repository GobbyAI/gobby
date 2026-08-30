"""REST MCP endpoint coverage for nested session_id reference resolution."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.servers.routes.mcp.endpoints.execution import call_mcp_tool
from tests.mcp_proxy.named_server_test_support import attach_named_servers

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
    attach_named_servers(mcp_manager, "gobby-sessions")
    # ToolProxyService.session_manager prefers the manager's session_manager;
    # leaving it as an auto-MagicMock would shadow this stub.
    mcp_manager.session_manager = session_manager

    internal_manager = MagicMock()
    internal_manager.is_internal.return_value = False

    hook_manager = SimpleNamespace(_session_manager=session_manager)

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
    server, mcp_manager = _make_server()
    request = _make_request()

    await call_mcp_tool(request, server)

    server.session_manager.resolve_session_reference.assert_any_call("#3", "proj-1")
    assert server.session_manager.resolve_session_reference.call_count >= 1
    assert server.session_manager.resolve_session_reference.call_args is not None
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-sessions",
        tool_name="get_session",
        arguments={"session_id": SESSION_UUID_3},
        session_id=SESSION_UUID_3,
        timeout=30.0,
    )
    assert mcp_manager.call_tool.await_count == 1
    assert mcp_manager.call_tool.await_args is not None


@pytest.mark.asyncio
async def test_rest_call_slow_session_lookup_does_not_block_concurrent_request() -> None:
    server, _ = _make_server()
    lookup_started = threading.Event()
    release_lookup = threading.Event()

    def slow_resolve(ref: str, project_id: str | None = None) -> str:
        if ref == "#3":
            lookup_started.set()
            release_lookup.wait(timeout=1)
        return SESSION_UUID_3

    server.session_manager.resolve_session_reference.side_effect = slow_resolve
    blocked_call = asyncio.create_task(call_mcp_tool(_make_request(), server))
    responsive_request = _make_request()
    responsive_request.headers = {}
    responsive_request.json = AsyncMock(
        return_value={
            "server_name": "test-server",
            "tool_name": "responsive-tool",
            "arguments": {},
        }
    )

    try:
        assert await asyncio.to_thread(lookup_started.wait, 0.5)
        await asyncio.wait_for(call_mcp_tool(responsive_request, server), timeout=0.2)
        assert not blocked_call.done()
    finally:
        release_lookup.set()
        await blocked_call
