"""Discovery keeps connection and listing budgets without extending other calls."""

import asyncio
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from gobby.mcp_proxy.client_manager import connections, server_registry, tool_inventory
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPError, MCPServerConfig
from gobby.mcp_proxy.stdio_proxy import DaemonProxy
from gobby.servers.routes.mcp.endpoints import discovery, execution, request_context
from gobby.servers.routes.mcp.endpoints import server as server_routes
from gobby.servers.routes.mcp.tools import create_mcp_router

if TYPE_CHECKING:
    from types import TracebackType

    from mcp import ClientSession

    from gobby.mcp_proxy.transports.base import BaseTransportConnection
    from gobby.servers.http import HTTPServer

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("operation", ["list", "schema", "all"])
async def test_external_discovery_http_budget_includes_server_connect_timeout(
    operation: str,
) -> None:
    proxy = DaemonProxy(60887)

    async def request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if path == "/api/mcp/servers":
            return {
                "success": True,
                "servers": [
                    {
                        "name": "slow",
                        "transport": "stdio",
                        "connect_timeout": 120.0,
                        "discovery_timeout": 165.0,
                    },
                ],
            }
        if path == "/api/admin/status":
            return {"success": True, "mcp_servers": {"slow": {}}}
        assert kwargs.get("session_id") == "caller-session"
        assert kwargs.get("timeout", 30.0) >= 165.0
        return {"success": True, "tools": [], "name": "read"}

    with patch.object(proxy, "_request", side_effect=request):
        if operation == "schema":
            result = await proxy.get_tool_schema("slow", "read", session_id="caller-session")
        else:
            result = await proxy.list_tools(
                "slow" if operation == "list" else None, session_id="caller-session"
            )
    assert result["success"] is True


async def test_other_proxy_requests_keep_existing_timeout_budgets() -> None:
    proxy = DaemonProxy(60887)
    proxy._tool_timeouts = {"custom": 17.0}
    request = AsyncMock(return_value={"success": True, "servers": []})
    with patch.object(proxy, "_request", request):
        await proxy.get_status()
        await proxy.call_tool("slow", "custom")
        await proxy.call_tool("slow", "ordinary")
    assert request.await_args_list[0].kwargs.get("timeout", 30.0) == 30.0
    assert request.await_args_list[1].kwargs["timeout"] == 17.0
    assert request.await_args_list[2].kwargs["timeout"] == 30.0
    assert all(call.args[1] != "/api/mcp/servers" for call in request.await_args_list)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.budgets: list[float] = []
        self.sleeps: list[float] = []

    async def wait_for(self, work: Coroutine[Any, Any, Any], timeout: float) -> Any:
        self.budgets.append(timeout)
        start = self.now
        result = await work
        if self.now - start > timeout:
            self.now = start + timeout
            raise TimeoutError
        return result

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay

    def timeout(self, delay: float) -> "_Deadline":
        self.budgets.append(delay)
        return _Deadline(self, delay)


class _Deadline:
    def __init__(self, clock: _Clock, delay: float) -> None:
        self.clock = clock
        self.end = clock.now + delay
        self.did_expire = False

    async def __aenter__(self) -> "_Deadline":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: "TracebackType | None",
    ) -> None:
        if self.clock.now > self.end:
            self.clock.now = self.end
            self.did_expire = True
            raise TimeoutError

    def expired(self) -> bool:
        return self.did_expire


@pytest.fixture(params=[(45.0, 45.0), (110.0, 35.0)])
def slow_inventory(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[MCPClientManager, MCPServerConfig, _Clock, SimpleNamespace]:
    connect_seconds, list_seconds = cast(tuple[float, float], request.param)
    clock = _Clock()
    config = MCPServerConfig(
        name="slow", project_id="test", transport="stdio", command="unused", connect_timeout=120
    )
    manager = MCPClientManager(max_connection_retries=0)
    tool = SimpleNamespace(name="read", description="Read a value", input_schema={"type": "object"})

    async def list_tools() -> Any:
        clock.now += list_seconds
        return SimpleNamespace(tools=[tool])

    session = SimpleNamespace(list_tools=AsyncMock(side_effect=list_tools))
    connection = SimpleNamespace(
        session=session, is_connected=True, disconnect=AsyncMock(), connect_seconds=connect_seconds
    )

    async def connect(cfg: MCPServerConfig) -> "ClientSession":
        clock.now += connect_seconds
        manager._connections[cfg.id] = cast("BaseTransportConnection", connection)
        return cast("ClientSession", session)

    monkeypatch.setattr(manager, "_connect_server", connect)
    for module in (tool_inventory, server_registry):
        monkeypatch.setattr(
            module, "asyncio", SimpleNamespace(timeout=clock.timeout, to_thread=asyncio.to_thread)
        )
    monkeypatch.setattr(
        server_registry, "time", SimpleNamespace(monotonic=lambda: clock.now), raising=False
    )
    return manager, config, clock, connection


async def test_per_server_http_route_allows_list_rpc_beyond_thirty_seconds(
    monkeypatch: pytest.MonkeyPatch,
    slow_inventory: tuple[MCPClientManager, MCPServerConfig, _Clock, SimpleNamespace],
) -> None:
    manager, config, clock, connection = slow_inventory
    manager.add_server_config(config)
    server = SimpleNamespace(mcp_manager=manager, _internal_manager=None, tool_proxy=None)
    app = FastAPI()
    app.include_router(create_mcp_router())
    app.state.server = server
    monkeypatch.setattr(execution, "_http_request_scope", lambda *args: "test")
    monkeypatch.setattr(execution, "_enforce_workflow_for_request", AsyncMock(return_value=False))
    monkeypatch.setattr(
        request_context,
        "_set_context_for_request",
        AsyncMock(return_value=SimpleNamespace(resolved_session_id=None)),
    )
    monkeypatch.setattr(request_context, "_reset_context", MagicMock())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/mcp/slow/tools")
    assert response.status_code == 200
    result = response.json()
    assert result["success"] is True, result
    assert result["tools"] == [
        {"name": "read", "description": "Read a value", "inputSchema": {"type": "object"}}
    ]
    assert clock.now <= config.connect_timeout + manager.connection_timeout
    assert clock.budgets == [150.0, 120.0]
    connection.session.list_tools.assert_awaited_once()
    connection.disconnect.assert_not_awaited()


async def test_schema_cache_miss_allows_list_rpc_beyond_thirty_seconds(
    slow_inventory: tuple[MCPClientManager, MCPServerConfig, _Clock, SimpleNamespace],
) -> None:
    manager, config, clock, connection = slow_inventory
    manager.add_server_config(config)
    assert config.id not in manager._tool_schema_cache
    schema = await manager.get_tool_input_schema(config.id, "read")
    assert schema == {"type": "object"}
    assert manager._tool_schema_cache[config.id][0]["name"] == "read"
    assert clock.now <= config.connect_timeout + manager.connection_timeout
    assert clock.budgets == [150.0, 120.0]
    connection.session.list_tools.assert_awaited_once()
    connection.disconnect.assert_not_awaited()


async def test_registry_initial_inventory_allows_list_rpc_beyond_thirty_seconds(
    slow_inventory: tuple[MCPClientManager, MCPServerConfig, _Clock, SimpleNamespace],
) -> None:
    manager, config, clock, connection = slow_inventory
    result = await manager.add_server(config)
    assert result["full_tool_schemas"] == [
        {"name": "read", "description": "Read a value", "inputSchema": {"type": "object"}}
    ]
    assert clock.now <= config.connect_timeout + manager.connection_timeout
    assert clock.budgets == [150.0 - connection.connect_seconds]
    assert manager._tool_schema_cache[config.id][0]["name"] == "read"
    connection.session.list_tools.assert_awaited_once()
    connection.disconnect.assert_not_awaited()


@pytest.mark.parametrize("stale_first", [False, True])
async def test_discovery_deadline_cancels_listing_and_cleans_exact_connection(
    monkeypatch: pytest.MonkeyPatch, stale_first: bool
) -> None:
    config = MCPServerConfig(
        name="blocked", project_id="test", transport="stdio", command="unused", connect_timeout=0.01
    )
    manager = MCPClientManager(
        server_configs=[config], connection_timeout=0.02, max_connection_retries=0
    )
    cancelled = asyncio.Event()

    async def blocked_listing() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    blocked = SimpleNamespace(
        session=SimpleNamespace(list_tools=AsyncMock(side_effect=blocked_listing)),
        is_connected=True,
        disconnect=AsyncMock(),
    )
    initial = SimpleNamespace(
        session=SimpleNamespace(
            list_tools=AsyncMock(
                side_effect=ConnectionError("stale") if stale_first else blocked_listing
            )
        ),
        is_connected=True,
        disconnect=AsyncMock(),
    )
    manager._connections[config.id] = cast("BaseTransportConnection", initial)

    async def reconnect(cfg: MCPServerConfig) -> "ClientSession":
        manager._connections[cfg.id] = cast("BaseTransportConnection", blocked)
        return cast("ClientSession", blocked.session)

    connect = AsyncMock(side_effect=reconnect)
    monkeypatch.setattr(manager, "_connect_server", connect)
    with pytest.raises(MCPError, match="Tool discovery timeout after 0.0"):
        await manager.list_tools(config.id)
    assert cancelled.is_set()
    assert config.id not in manager._connections
    assert config.id not in manager._tool_schema_cache
    blocked.disconnect.assert_awaited_once()
    initial.disconnect.assert_awaited_once()
    connect.assert_awaited_once()


async def test_discovery_connects_after_thirty_seconds_of_simulated_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    session = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])))

    async def connect(name: str) -> Any:
        assert name == "slow-id"
        clock.now += 45.0
        return session

    manager = SimpleNamespace(
        _configs={"slow-id": SimpleNamespace(connect_timeout=120.0)},
        ensure_connected=connect,
        cache_discovered_tools=MagicMock(),
    )
    monkeypatch.setattr(discovery, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(
        discovery,
        "asyncio",
        SimpleNamespace(
            wait_for=clock.wait_for,
            to_thread=AsyncMock(),
        ),
    )
    result = await discovery._list_external_server_tools(manager, "slow-id", timeout=30.0)
    assert result == []
    assert clock.budgets == [120.0, 105.0]
    session.list_tools.assert_awaited_once()


async def test_registry_timeout_log_names_server_and_elapsed_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = MCPServerConfig(name="slow", transport="stdio", command="unused", project_id="test")
    manager = SimpleNamespace(connection_timeout=42.0, _connections={})
    session = SimpleNamespace(list_tools=AsyncMock(side_effect=TimeoutError))
    result = await server_registry._discover_and_cache_tools(manager, config, session)
    assert result == []
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "slow" in message and "72.0" in message and "tim" in message.lower() for message in messages
    )


async def test_dead_server_first_contact_exhausts_one_budget_without_retry_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    config = MCPServerConfig(
        name="dead", transport="stdio", command="unused", connect_timeout=40, project_id="test"
    )

    async def connect(cfg: MCPServerConfig) -> None:
        clock.now += cfg.connect_timeout
        raise TimeoutError

    retry = SimpleNamespace(max_retries=2, get_delay=lambda attempt: 5.0)
    connector = SimpleNamespace(
        retry_config=retry,
        get_state=lambda name: None,
        mark_failed=MagicMock(),
    )
    manager = SimpleNamespace(_lazy_connector=connector, _connect_server=connect)
    monkeypatch.setattr(connections, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(
        connections,
        "asyncio",
        SimpleNamespace(
            wait_for=clock.wait_for,
            sleep=clock.sleep,
        ),
    )
    with pytest.raises(MCPError, match="dead"):
        await connections._connect_with_retries(manager, config.id, config)
    assert clock.now <= config.connect_timeout
    assert clock.budgets == [40.0]
    assert clock.sleeps == []


async def test_server_inventory_publishes_connection_plus_listing_budget() -> None:
    config = MCPServerConfig(
        name="slow", project_id="test", transport="stdio", command="unused", connect_timeout=120
    )
    manager = SimpleNamespace(
        server_configs=[config],
        health={},
        connection_timeout=45.0,
        is_connected=lambda server_id: False,
    )
    server = SimpleNamespace(
        _internal_manager=None,
        mcp_manager=manager,
        config=SimpleNamespace(mcp_client_proxy=SimpleNamespace(tool_timeout=60.0)),
    )
    request = Request({"type": "http", "method": "GET", "path": "/api/mcp/servers", "headers": []})
    with (
        patch.object(server_routes, "request_mcp_scope", return_value="test"),
        patch.object(server_routes, "list_mcp_templates", return_value={"templates": []}),
    ):
        result = await server_routes.list_mcp_servers(request, cast("HTTPServer", server))
    assert result["success"] is True
    assert result["servers"][0]["connect_timeout"] == 120.0
    assert result["servers"][0]["discovery_timeout"] == 180.0


async def test_fast_connection_retries_share_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    config = MCPServerConfig(
        name="flaky", project_id="test", transport="stdio", command="unused", connect_timeout=40
    )
    session = object()
    calls = 0

    async def connect(cfg: MCPServerConfig) -> object:
        nonlocal calls
        calls += 1
        clock.now += 10.0
        if calls == 1:
            raise MCPError("transient")
        return session

    connector = SimpleNamespace(
        retry_config=SimpleNamespace(max_retries=2, get_delay=lambda attempt: 5.0),
        get_state=lambda name: None,
        mark_failed=MagicMock(),
        mark_connected=MagicMock(),
    )
    manager = SimpleNamespace(_lazy_connector=connector, _connect_server=connect)
    monkeypatch.setattr(connections, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(
        connections,
        "asyncio",
        SimpleNamespace(
            wait_for=clock.wait_for,
            sleep=clock.sleep,
        ),
    )
    result = await connections._connect_with_retries(manager, config.id, config)
    assert result is session
    assert calls == 2
    assert clock.budgets == [40.0, 25.0]
    assert clock.sleeps == [5.0]
    assert clock.now == 25.0
