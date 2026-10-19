"""Discovery keeps connection and listing budgets without extending other calls."""

from collections.abc import Coroutine
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from gobby.mcp_proxy.client_manager import connections, server_registry
from gobby.mcp_proxy.models import MCPError, MCPServerConfig
from gobby.mcp_proxy.stdio_proxy import DaemonProxy
from gobby.servers.routes.mcp.endpoints import discovery
from gobby.servers.routes.mcp.endpoints import server as server_routes

if TYPE_CHECKING:
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
        "slow" in message and "42.0" in message and "tim" in message.lower() for message in messages
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
