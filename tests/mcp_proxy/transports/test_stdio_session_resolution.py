"""Stdio-to-REST MCP endpoint coverage for nested session_id resolution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.servers.routes.mcp.endpoints.execution import mcp_proxy

pytestmark = pytest.mark.unit


SESSION_UUID_3 = "33333333-3333-4333-8333-333333333333"
SESSION_UUID_4 = "44444444-4444-4444-8444-444444444444"
PROJECT_ID = "proj-1"


class _FakeDb:
    def fetchall(self, query: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        if "FROM sessions" in query and "seq_num" in query and params == (4,):
            return [{"project_id": PROJECT_ID}]
        return []

    def fetchone(self, query: str, params: tuple[object, ...] = ()) -> dict[str, object] | None:
        if "FROM projects" in query and params == (PROJECT_ID,):
            return {
                "id": PROJECT_ID,
                "name": "test-project",
                "repo_path": None,
                "github_url": None,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        return None


def _make_session_manager(db: object | None = None) -> MagicMock:
    session_manager = MagicMock()
    session_manager.db = db
    session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: (
        SESSION_UUID_3 if ref in {"#3", SESSION_UUID_3} else SESSION_UUID_4
    )
    session_manager.get.return_value = SimpleNamespace(
        external_id="ext-session-3",
        project_id=PROJECT_ID,
    )
    return session_manager


def _make_server(db: object | None = None) -> tuple[MagicMock, MagicMock]:
    session_manager = _make_session_manager(db)
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


def _make_stdio_request(
    *,
    project_id: str | None = PROJECT_ID,
    wrapper_session_id: str | None = "wrapper-session",
    target_session_id: str = "#3",
) -> MagicMock:
    request = MagicMock()
    request.headers = {}
    if project_id:
        request.headers["x-gobby-project-id"] = project_id
    if wrapper_session_id:
        request.headers["x-gobby-session-id"] = wrapper_session_id
    request.json = AsyncMock(return_value={"session_id": target_session_id})
    return request


@pytest.mark.asyncio
async def test_stdio_rest_path_resolves_target_session_but_uses_wrapper_context() -> None:
    server, mcp_manager = _make_server()
    request = _make_stdio_request()

    await mcp_proxy("gobby-sessions", "get_session", request, server)

    server.session_manager.resolve_session_reference.assert_any_call("#3", "proj-1")
    assert server.session_manager.resolve_session_reference.call_count >= 1
    assert server.session_manager.resolve_session_reference.call_args is not None
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-sessions",
        "get_session",
        {"session_id": SESSION_UUID_3},
        session_id=SESSION_UUID_4,
    )
    assert mcp_manager.call_tool.await_count == 1
    assert mcp_manager.call_tool.await_args is not None


@pytest.mark.asyncio
async def test_stdio_resolves_hash_ref_from_header_hash_ref_without_project_header() -> None:
    server, mcp_manager = _make_server(db=_FakeDb())
    request = _make_stdio_request(project_id=None, wrapper_session_id="#4", target_session_id="#3")

    await mcp_proxy("gobby-sessions", "get_session", request, server)

    server.session_manager.resolve_session_reference.assert_any_call("#3", PROJECT_ID)
    assert server.session_manager.resolve_session_reference.call_count >= 1
    assert server.session_manager.resolve_session_reference.call_args is not None
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-sessions",
        "get_session",
        {"session_id": SESSION_UUID_3},
        session_id=SESSION_UUID_4,
    )
    assert mcp_manager.call_tool.await_count == 1
    assert mcp_manager.call_tool.await_args is not None
