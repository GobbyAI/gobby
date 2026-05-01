"""Stale session recovery tests for MCPClientManager."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import anyio
import pytest

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import ConnectionState, MCPConnectionHealth, MCPError, MCPServerConfig

pytestmark = pytest.mark.unit


class FakeSession:
    def __init__(self, tools: list[SimpleNamespace] | None = None) -> None:
        self._tools = tools or []

    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(tools=self._tools)


class ClosedSession:
    async def list_tools(self) -> SimpleNamespace:
        raise anyio.ClosedResourceError


class FakeConnection:
    def __init__(
        self,
        session: FakeSession | ClosedSession,
        connect_error: Exception | None = None,
    ) -> None:
        self.session = session
        self.connect_error = connect_error
        self.is_connected = True
        self.disconnect_calls = 0

    async def connect(self) -> FakeSession | ClosedSession:
        if self.connect_error:
            raise self.connect_error
        self.is_connected = True
        return self.session

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False


def make_manager() -> MCPClientManager:
    config = MCPServerConfig(
        name="external",
        project_id="test-project",
        transport="stdio",
        command="example-mcp",
    )
    manager = MCPClientManager(server_configs=[config], max_connection_retries=0)
    manager.health["external"] = MCPConnectionHealth(
        name="external",
        state=ConnectionState.CONNECTED,
    )
    manager._lazy_connector.mark_connected("external")
    return manager


@pytest.mark.asyncio
async def test_single_server_list_tools_reconnects_after_closed_session() -> None:
    manager = make_manager()
    stale_connection = FakeConnection(ClosedSession())
    manager._connections["external"] = stale_connection

    fresh_tool = SimpleNamespace(
        name="fresh_tool",
        description="Fresh tool",
        inputSchema={"type": "object"},
    )
    fresh_connection = FakeConnection(FakeSession([fresh_tool]))

    with patch(
        "gobby.mcp_proxy.manager.create_transport_connection",
        return_value=fresh_connection,
    ):
        result = await manager.list_tools("external")

    assert result == {
        "external": [
            {
                "name": "fresh_tool",
                "description": "Fresh tool",
                "inputSchema": {"type": "object"},
            }
        ]
    }
    assert stale_connection.disconnect_calls == 1
    assert manager._connections["external"] is fresh_connection


@pytest.mark.asyncio
async def test_schema_lookup_reports_reconnect_failure_not_missing_tool() -> None:
    manager = make_manager()
    manager._connections["external"] = FakeConnection(ClosedSession())
    failing_connection = FakeConnection(
        FakeSession(),
        connect_error=RuntimeError("connect failed"),
    )

    with patch(
        "gobby.mcp_proxy.manager.create_transport_connection",
        return_value=failing_connection,
    ):
        with pytest.raises(MCPError) as exc_info:
            await manager.get_tool_input_schema("external", "fresh_tool")

    message = str(exc_info.value)
    assert "initial listing failed: ClosedResourceError" in message
    assert "reconnect retry failed" in message
    assert "Tool fresh_tool not found" not in message
