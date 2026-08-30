"""Stale session recovery tests for MCPClientManager."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import anyio
import pytest

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import ConnectionState, MCPConnectionHealth, MCPError, MCPServerConfig
from gobby.mcp_proxy.transports.base import BaseTransportConnection

pytestmark = pytest.mark.unit


class FakeSession:
    def __init__(
        self,
        tools: list[SimpleNamespace] | None = None,
        tool_result: object | None = None,
    ) -> None:
        self._tools = tools or []
        self._tool_result = tool_result

    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        return self._tool_result


class ClosedSession:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or anyio.ClosedResourceError()

    async def list_tools(self) -> SimpleNamespace:
        raise self._error

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        raise self._error


class FakeConnection(BaseTransportConnection):
    def __init__(
        self,
        session: FakeSession | ClosedSession,
        connect_error: Exception | None = None,
    ) -> None:
        super().__init__(
            MCPServerConfig(
                name="external",
                project_id="test-project",
                transport="stdio",
                command="example-mcp",
            )
        )
        self._fake_session = session
        self._session = session
        self._state = ConnectionState.CONNECTED
        self.connect_error = connect_error
        self.disconnect_calls = 0

    async def connect(self) -> FakeSession | ClosedSession:
        if self.connect_error:
            raise self.connect_error
        self._session = self._fake_session
        self._state = ConnectionState.CONNECTED
        return self._fake_session

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._session = None
        self._state = ConnectionState.DISCONNECTED


def make_manager() -> tuple[MCPClientManager, MCPServerConfig]:
    config = MCPServerConfig(
        name="external",
        project_id="test-project",
        transport="stdio",
        command="example-mcp",
    )
    manager = MCPClientManager(server_configs=[config], max_connection_retries=0)
    manager.health[config.id] = MCPConnectionHealth(
        name="external",
        state=ConnectionState.CONNECTED,
        project_id=config.project_id,
    )
    manager._lazy_connector.mark_connected(config.id)
    return manager, config


@pytest.mark.asyncio
async def test_single_server_list_tools_reconnects_after_closed_session() -> None:
    manager, config = make_manager()
    stale_connection = FakeConnection(ClosedSession())
    manager._connections[config.id] = stale_connection

    fresh_tool = SimpleNamespace(
        name="fresh_tool",
        description="Fresh tool",
        input_schema={"type": "object"},
    )
    fresh_connection = FakeConnection(FakeSession([fresh_tool]))

    with patch(
        "gobby.mcp_proxy.manager.create_transport_connection",
        return_value=fresh_connection,
    ):
        result = await manager.list_tools(config.id)

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
    assert manager._connections[config.id] is fresh_connection


@pytest.mark.parametrize(
    "error_type",
    [anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream],
)
async def test_call_tool_reconnects_after_dead_session(
    error_type: type[Exception],
) -> None:
    manager, config = make_manager()
    stale_connection = FakeConnection(ClosedSession(error_type()))
    manager._connections[config.id] = stale_connection

    expected = {"result": "fresh"}
    fresh_connection = FakeConnection(FakeSession(tool_result=expected))

    with patch(
        "gobby.mcp_proxy.manager.create_transport_connection",
        return_value=fresh_connection,
    ):
        result = await manager.call_tool(config.id, "fresh_tool", {"value": 1})

    assert result == expected
    assert stale_connection.disconnect_calls == 1
    assert manager._connections[config.id] is fresh_connection


async def test_call_tool_retries_dead_session_only_once() -> None:
    manager, config = make_manager()
    stale_connection = FakeConnection(ClosedSession())
    manager._connections[config.id] = stale_connection
    retry_connection = FakeConnection(ClosedSession(anyio.BrokenResourceError()))

    with patch(
        "gobby.mcp_proxy.manager.create_transport_connection",
        return_value=retry_connection,
    ) as create_connection:
        with pytest.raises(anyio.BrokenResourceError):
            await manager.call_tool(config.id, "still_dead", {})

    create_connection.assert_called_once()
    assert stale_connection.disconnect_calls == 1
    assert retry_connection.disconnect_calls == 0


@pytest.mark.asyncio
async def test_schema_lookup_reports_reconnect_failure_not_missing_tool() -> None:
    manager, config = make_manager()
    manager._connections[config.id] = FakeConnection(ClosedSession())
    failing_connection = FakeConnection(
        FakeSession(),
        connect_error=RuntimeError("connect failed"),
    )

    with patch(
        "gobby.mcp_proxy.manager.create_transport_connection",
        return_value=failing_connection,
    ):
        with pytest.raises(MCPError) as exc_info:
            await manager.get_tool_input_schema(config.id, "fresh_tool")

    message = str(exc_info.value)
    assert "initial listing failed: ClosedResourceError" in message
    assert "reconnect retry failed" in message
    assert "Tool fresh_tool not found" not in message
