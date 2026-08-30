"""Resolved wrapper ownership across the four MCP execution endpoints."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.wait_tools import (
    MCP_WRAPPER_PROTOCOL_VERSION,
    MCP_WRAPPER_PROTOCOL_VERSION_HEADER,
)
from gobby.servers.routes.mcp.endpoints.execution import (
    call_mcp_tool,
    get_tool_schema,
    list_mcp_tools,
    mcp_proxy,
)
from gobby.utils.session_context import (
    TERMINAL_CONTEXT_HEADER,
    SeededContextTokens,
    get_current_session_id,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

RESOLVED_SESSION_ID = str(uuid.uuid4())
PROJECT_ID = str(uuid.uuid4())
TERMINAL_CONTEXT = {"parent_pid": 4242, "tmux_pane": "%12"}


def _make_server() -> MagicMock:
    server = MagicMock()
    server.session_manager.resolve_current_terminal_session.return_value = SimpleNamespace(
        id=RESOLVED_SESSION_ID,
        external_id="external-session",
        project_id=PROJECT_ID,
    )
    server.run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
    return server


def _make_request(body: dict[str, Any]) -> MagicMock:
    request = MagicMock()
    request.headers = {
        MCP_WRAPPER_PROTOCOL_VERSION_HEADER: MCP_WRAPPER_PROTOCOL_VERSION,
        TERMINAL_CONTEXT_HEADER: json.dumps(TERMINAL_CONTEXT),
        "x-gobby-caller-project-id": PROJECT_ID,
    }
    request.query_params = {}
    request.json = AsyncMock(return_value=body)
    return request


async def test_list_tools_attributes_discovery_to_resolved_session() -> None:
    server = _make_server()
    registry = MagicMock()

    def list_tools() -> list[dict[str, Any]]:
        assert get_current_session_id() == RESOLVED_SESSION_ID
        return []

    registry.list_tools.side_effect = list_tools
    internal_manager = MagicMock()
    internal_manager.is_internal.return_value = True
    internal_manager.get_registry.return_value = registry

    with patch(
        "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
        return_value=SeededContextTokens(resolved_project_id=PROJECT_ID),
    ):
        await list_mcp_tools(
            "gobby-memory",
            _make_request({}),
            server,
            internal_manager,
            None,
        )

    server.tool_proxy.record_listed_server.assert_called_once_with(
        "gobby-memory",
        session_id=RESOLVED_SESSION_ID,
    )


async def test_get_schema_executes_under_resolved_session() -> None:
    server = _make_server()
    registry = MagicMock()

    def get_schema(tool_name: str) -> dict[str, Any]:
        assert get_current_session_id() == RESOLVED_SESSION_ID
        return {"name": tool_name, "inputSchema": {"type": "object"}}

    registry.get_schema.side_effect = get_schema
    server._internal_manager.is_internal.return_value = True
    server._internal_manager.get_registry.return_value = registry
    body = {"server_name": "gobby-memory", "tool_name": "search_memories"}

    with patch(
        "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
        return_value=SeededContextTokens(resolved_project_id=PROJECT_ID),
    ):
        result = await get_tool_schema(_make_request(body), server)

    assert result["success"] is True
    registry.get_schema.assert_called_once_with("search_memories")


@pytest.mark.parametrize(
    ("server_name", "tool_name"),
    [
        ("gobby", "call_tool"),
        ("?", "mcp__gobby__call_tool"),
    ],
)
async def test_get_schema_does_not_probe_unconfigured_proxy_namespace(
    server_name: str, tool_name: str
) -> None:
    server = _make_server()
    server._internal_manager.is_internal.return_value = False
    server.mcp_manager.has_server.return_value = False
    server.tool_proxy.get_tool_schema = AsyncMock(
        return_value={
            "success": False,
            "error": "server_name='gobby' is not a real server",
        }
    )

    with patch(
        "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
        return_value=SeededContextTokens(resolved_project_id=PROJECT_ID),
    ):
        result = await get_tool_schema(
            _make_request({"server_name": server_name, "tool_name": tool_name}),
            server,
        )

    assert result["success"] is False
    server.mcp_manager.get_tool_info.assert_not_called()
    server.tool_proxy.get_tool_schema.assert_awaited_once_with(
        "gobby", "call_tool", project_id=PROJECT_ID
    )


@pytest.mark.parametrize("endpoint", ["call", "proxy"])
async def test_tool_calls_use_resolved_session_ownership(endpoint: str) -> None:
    server = _make_server()

    async def call_tool(
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert server_name == "gobby-memory"
        assert tool_name == "search_memories"
        assert arguments == {"query": "stateless"}
        assert get_current_session_id() == RESOLVED_SESSION_ID
        assert kwargs["session_id"] == RESOLVED_SESSION_ID
        return {"memories": []}

    server.tool_proxy.call_tool = AsyncMock(side_effect=call_tool)
    arguments = {"query": "stateless"}

    with patch(
        "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
        return_value=SeededContextTokens(resolved_project_id=PROJECT_ID),
    ):
        if endpoint == "call":
            body = {
                "server_name": "gobby-memory",
                "tool_name": "search_memories",
                "arguments": arguments,
            }
            result = await call_mcp_tool(_make_request(body), server)
        else:
            result = await mcp_proxy(
                "gobby-memory",
                "search_memories",
                _make_request(arguments),
                server,
            )

    assert result["success"] is True
    server.tool_proxy.call_tool.assert_awaited_once()
