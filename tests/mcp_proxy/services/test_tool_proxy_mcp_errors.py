"""ToolProxyService MCP manager error handling tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.models import MCPError
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_list_tools_returns_structured_error_for_manager_mcp_error() -> None:
    manager = MagicMock()
    manager.project_id = "test-project"
    manager.has_server.return_value = True
    manager.list_tools = AsyncMock(
        side_effect=MCPError("Failed to list tools for server 'external': reconnect retry failed")
    )

    internal_manager = MagicMock()
    internal_manager.is_internal.return_value = False

    proxy = ToolProxyService(
        mcp_manager=manager,
        internal_manager=internal_manager,
    )

    result = await proxy.list_tools("external")

    assert result["success"] is False
    assert result["tools"] == []
    assert "reconnect retry failed" in result["error"]
