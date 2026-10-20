"""Stale session recovery tests for MCPClientManager."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from gobby.mcp_proxy.client_manager.server_registry import _discover_and_cache_tools
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


class HangingSession(FakeSession):
    async def list_tools(self) -> SimpleNamespace:
        await asyncio.Event().wait()
        return await super().list_tools()


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
        disconnect_error: Exception | None = None,
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
        self.disconnect_error = disconnect_error

    async def connect(self) -> FakeSession | ClosedSession:
        if self.connect_error:
            raise self.connect_error
        self._session = self._fake_session
        self._state = ConnectionState.CONNECTED
        return self._fake_session

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.disconnect_error:
            raise self.disconnect_error
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
@pytest.mark.parametrize("schema_lookup", [False, True])
async def test_first_contact_timeout_does_not_retry_as_a_stale_session(
    monkeypatch: pytest.MonkeyPatch, schema_lookup: bool
) -> None:
    manager, config = make_manager()
    config.connect_timeout = 0.01
    manager.connection_timeout = 0.05

    async def hang_connect(_: MCPServerConfig) -> None:
        await asyncio.Event().wait()

    connect = AsyncMock(side_effect=hang_connect)
    monkeypatch.setattr(manager, "_connect_server", connect)
    with pytest.raises(MCPError, match="Failed to connect") as exc_info:
        if schema_lookup:
            await manager.get_tool_input_schema(config.id, "fresh_tool")
        else:
            await manager.list_tools(config.id)

    connect.assert_awaited_once_with(config)
    assert "reconnect retry failed" not in str(exc_info.value)
    assert "Tool discovery timeout" not in str(exc_info.value)
    assert config.id not in manager._connections


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


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_times_out", [False, True])
async def test_schema_lookup_recovers_hung_session(cleanup_times_out: bool) -> None:
    manager, config = make_manager()
    manager.connection_timeout = 0.01
    config.connect_timeout = 0.01
    stale_connection = FakeConnection(
        HangingSession(),
        disconnect_error=TimeoutError("owner cleanup timed out") if cleanup_times_out else None,
    )
    manager._connections[config.id] = stale_connection
    fresh_connection = FakeConnection(
        FakeSession(
            [
                SimpleNamespace(
                    name="fresh_tool", description="Fresh", input_schema={"type": "object"}
                )
            ]
        )
    )

    with patch(
        "gobby.mcp_proxy.manager.create_transport_connection",
        return_value=fresh_connection,
    ) as factory:
        lookup = asyncio.create_task(manager.get_tool_input_schema(config.id, "fresh_tool"))
        try:
            done, _ = await asyncio.wait({lookup}, timeout=0.5)
            assert lookup in done, "Hung discovery must reconnect instead of waiting indefinitely"
            assert await lookup == {"type": "object"}
        finally:
            lookup.cancel()
            with suppress(asyncio.CancelledError):
                await lookup

    factory.assert_called_once()
    assert stale_connection.disconnect_calls == 1
    assert manager._connections[config.id] is fresh_connection
    assert manager.health[config.id].state is ConnectionState.CONNECTED


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_times_out", [False, True])
async def test_schema_lookup_discards_hung_session_after_retry(initial_times_out: bool) -> None:
    manager, config = make_manager()
    manager.connection_timeout = 0.01
    config.connect_timeout = 0.01
    stale_connection = FakeConnection(HangingSession() if initial_times_out else ClosedSession())
    manager._connections[config.id] = stale_connection
    retry_connection = FakeConnection(HangingSession())

    with patch(
        "gobby.mcp_proxy.manager.create_transport_connection",
        return_value=retry_connection,
    ) as factory:
        lookup = asyncio.create_task(manager.get_tool_input_schema(config.id, "fresh_tool"))
        try:
            done, _ = await asyncio.wait({lookup}, timeout=0.5)
            assert lookup in done, "A hung reconnect must fail within the discovery deadline"
            with pytest.raises(MCPError) as exc_info:
                await lookup
            message = str(exc_info.value)
            if initial_times_out:
                assert "Tool discovery timeout after 0.02s" in message
            else:
                assert "reconnect retry failed: Tool discovery timeout after 0.01s" in message
        finally:
            lookup.cancel()
            with suppress(asyncio.CancelledError, MCPError):
                await lookup

    factory.assert_called_once()
    assert stale_connection.disconnect_calls == 1
    assert retry_connection.disconnect_calls == 1
    assert config.id not in manager._connections
    assert not manager._lazy_connector.is_connected(config.id)


@pytest.mark.asyncio
async def test_registry_discovery_discards_hung_sibling_session() -> None:
    manager, config = make_manager()
    manager.connection_timeout = 0.01
    config.connect_timeout = 0.01
    stale_connection = FakeConnection(HangingSession())
    manager._connections[config.id] = stale_connection
    lock = manager._lazy_connector.get_connection_lock(config.id)

    async with lock:
        discovery = asyncio.create_task(
            _discover_and_cache_tools(manager, config, stale_connection._fake_session)
        )
        try:
            done, _ = await asyncio.wait({discovery}, timeout=0.5)
            assert discovery in done, "Lifecycle discovery must bound a hung session under its lock"
            assert await discovery == []
        finally:
            discovery.cancel()
            with suppress(asyncio.CancelledError):
                await discovery

    assert stale_connection.disconnect_calls == 1
    assert config.id not in manager._connections
    assert config.id not in manager._tool_schema_cache
    assert not manager._lazy_connector.is_connected(config.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["list", "refresh"])
async def test_http_discovery_recovers_hung_sibling_session(endpoint: str) -> None:
    from gobby.servers.routes.mcp.endpoints.execution import list_mcp_tools
    from gobby.servers.routes.mcp.endpoints.registry import refresh_mcp_tools

    manager, config = make_manager()
    manager.connection_timeout = 0.01
    config.connect_timeout = 0.01
    stale_connection = FakeConnection(HangingSession())
    manager._connections[config.id] = stale_connection
    fresh_connection = FakeConnection(
        FakeSession(
            [
                SimpleNamespace(
                    name="fresh_tool", description="Fresh", input_schema={"type": "object"}
                )
            ]
        )
    )
    server = MagicMock()
    server.mcp_manager = manager
    server._internal_manager = None
    server._tools_handler = None
    server.tool_proxy = None
    server._mcp_db_manager.get_cached_tools.return_value = []
    request = MagicMock()
    request.json = AsyncMock(return_value={"server_id": config.id})

    with (
        patch("gobby.mcp_proxy.manager.create_transport_connection", return_value=fresh_connection),
        patch.object(manager, "refresh_server", new=AsyncMock()),
        patch(
            "gobby.servers.routes.mcp.endpoints.request_context._set_context_for_request",
            new=AsyncMock(return_value=SimpleNamespace(resolved_session_id=None)),
        ),
        patch("gobby.servers.routes.mcp.endpoints.request_context._reset_context"),
        patch(
            "gobby.servers.routes.mcp.endpoints.execution._enforce_workflow_for_request",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "gobby.servers.routes.mcp.endpoints.execution._http_request_scope",
            return_value=config.project_id,
        ),
        patch(
            "gobby.servers.routes.mcp.endpoints.request_context.request_mcp_scope",
            return_value=config.project_id,
        ),
        patch("gobby.mcp_proxy.schema_hash.SchemaHashManager") as hash_manager,
    ):
        hash_manager.return_value.check_tools_for_changes.return_value = {
            "new": ["fresh_tool"],
            "changed": [],
            "unchanged": [],
        }
        hash_manager.return_value.cleanup_stale_hashes.return_value = 0
        if endpoint == "list":
            operation = list_mcp_tools(config.name, request, server, None, manager)
        else:
            operation = refresh_mcp_tools(request, server)
        discovery = asyncio.create_task(operation)
        try:
            done, _ = await asyncio.wait({discovery}, timeout=0.5)
            assert discovery in done, (
                "HTTP discovery must recover instead of retaining a hung session"
            )
            result = await discovery
        finally:
            discovery.cancel()
            with suppress(asyncio.CancelledError):
                await discovery

    assert result["success"] is True
    if endpoint == "list":
        assert result["tools"] == [
            {"name": "fresh_tool", "description": "Fresh", "inputSchema": {"type": "object"}}
        ]
    else:
        assert result["stats"]["servers_processed"] == 1
        assert result["stats"]["tools_new"] == 1
    assert stale_connection.disconnect_calls == 1
    assert manager._connections[config.id] is fresh_connection
