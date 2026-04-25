"""Regression tests for call_tool wrapper versus target session_id handling."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.services.tool_proxy import ToolProxyService

pytestmark = pytest.mark.unit


SESSION_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string"},
        "limit": {"type": "integer"},
        "lines": {"type": "integer"},
    },
    "required": ["session_id"],
}


@pytest.fixture
def tool_proxy() -> tuple[ToolProxyService, MagicMock]:
    mcp_manager = MagicMock()
    mcp_manager.project_id = "proj-1"
    mcp_manager.session_manager = None
    mcp_manager.call_tool = AsyncMock(return_value={"success": True, "ok": True})

    internal_manager = MagicMock()
    internal_manager.is_internal.return_value = False

    proxy = ToolProxyService(
        mcp_manager=mcp_manager,
        internal_manager=internal_manager,
        validate_arguments=True,
    )
    proxy._emit_synthetic_after_tool = AsyncMock()
    return proxy, mcp_manager


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("get_session", {}),
        ("capture_output", {"lines": 10}),
        ("get_session_messages", {"limit": 25}),
    ],
)
async def test_top_level_only_session_id_returns_target_argument_error(
    tool_proxy: tuple[ToolProxyService, MagicMock],
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    proxy, mcp_manager = tool_proxy

    async def get_tool_schema(server_name: str, requested_tool: str) -> dict[str, Any]:
        assert server_name == "gobby-sessions"
        assert requested_tool == tool_name
        return {"success": True, "tool": {"inputSchema": SESSION_ID_SCHEMA}}

    proxy.get_tool_schema = get_tool_schema  # type: ignore[method-assign]

    result = await proxy.call_tool(
        "gobby-sessions",
        tool_name,
        arguments=arguments,
        session_id="wrapper-session",
        enforce_workflow=False,
    )

    assert result["success"] is False
    assert "arguments.session_id" in result["error"]
    assert "top-level call_tool.session_id is Gobby wrapper context only" in result["error"]
    assert result["schema"] == SESSION_ID_SCHEMA
    mcp_manager.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_arguments_session_id_stays_in_target_arguments(
    tool_proxy: tuple[ToolProxyService, MagicMock],
) -> None:
    proxy, mcp_manager = tool_proxy

    async def get_tool_schema(server_name: str, tool_name: str) -> dict[str, Any]:
        return {"success": True, "tool": {"inputSchema": SESSION_ID_SCHEMA}}

    proxy.get_tool_schema = get_tool_schema  # type: ignore[method-assign]

    result = await proxy.call_tool(
        "gobby-sessions",
        "get_session",
        arguments={"session_id": "target-session"},
        session_id="wrapper-session",
        enforce_workflow=False,
    )

    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-sessions",
        "get_session",
        {"session_id": "target-session"},
        session_id="wrapper-session",
    )
