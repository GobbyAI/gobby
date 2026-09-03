"""Regression tests for non-blocking stdio bridge startup."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from mcp import ClientSession
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams

from gobby.config.bootstrap import BootstrapConfig
from gobby.mcp_proxy.stdio_daemon import DaemonStartupDependencies
from gobby.mcp_proxy.stdio_daemon import ensure_daemon_running as ensure_stdio_daemon_running
from gobby.mcp_proxy.stdio_daemon import main as run_stdio_bridge
from gobby.mcp_proxy.stdio_proxy import DaemonProxy, DaemonProxyDependencies


def _proxy_dependencies(
    *,
    check_health: AsyncMock,
    client: AsyncMock,
) -> DaemonProxyDependencies:
    runtime = MagicMock()
    runtime.require_config.return_value.mcp_client_proxy.tool_timeouts = {}
    return DaemonProxyDependencies(
        runtime_factory=lambda: runtime,
        check_daemon_http_health=check_health,
        read_project_id=lambda: None,
        http_client_factory=lambda: client,
        logger=logging.getLogger(__name__),
    )


class _MemoryMCPServer(MCPServer[None]):
    def __init__(
        self,
        *,
        startup_task: asyncio.Task[None],
        health_waiting: asyncio.Event,
        release_health: asyncio.Event,
        initialize_answered: asyncio.Event,
    ) -> None:
        super().__init__("test-stdio-startup")
        self.startup_task = startup_task
        self.health_waiting = health_waiting
        self.release_health = release_health
        self.initialize_answered = initialize_answered

    async def run_stdio_async(self) -> None:
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            server_task = asyncio.create_task(
                self._lowlevel_server.run(
                    *server_streams,
                    self._lowlevel_server.create_initialization_options(),
                )
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await asyncio.wait_for(self.health_waiting.wait(), timeout=1)
                    initialize_result = await asyncio.wait_for(session.initialize(), timeout=1)
                    tools_result = await asyncio.wait_for(session.list_tools(), timeout=1)

                    assert initialize_result.server_info.name == "test-stdio-startup"
                    assert tools_result.tools == []
                    assert not self.startup_task.done()

                    self.initialize_answered.set()
                    self.release_health.set()
                    await asyncio.wait_for(asyncio.shield(self.startup_task), timeout=1)
            finally:
                server_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await server_task


@pytest.mark.asyncio
async def test_initialize_answered_before_daemon_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOBBY_AGENT_RUN_ID", raising=False)
    health_waiting = asyncio.Event()
    release_health = asyncio.Event()
    initialize_answered = asyncio.Event()

    async def check_health(
        port: int,
        timeout: float = 2.0,
        *,
        base_url: str | None = None,
    ) -> bool:
        del port, timeout, base_url
        health_waiting.set()
        await release_health.wait()
        return True

    deps = DaemonStartupDependencies(
        bootstrap=BootstrapConfig(daemon_port=60887, websocket_port=60888),
        is_daemon_running=lambda: False,
        get_daemon_pid=lambda: None,
        check_daemon_http_health=check_health,
        start_daemon_process=AsyncMock(return_value={"success": True}),
        logger=logging.getLogger(__name__),
    )

    def create_server(*, startup_task: asyncio.Task[None]) -> MCPServer[None]:
        return _MemoryMCPServer(
            startup_task=startup_task,
            health_waiting=health_waiting,
            release_health=release_health,
            initialize_answered=initialize_answered,
        )

    await run_stdio_bridge(deps=deps, create_server=create_server)

    assert initialize_answered.is_set()


@pytest.mark.asyncio
async def test_tool_call_awaits_lazy_daemon_startup() -> None:
    startup_waiting = asyncio.Event()
    release_startup = asyncio.Event()

    async def wait_for_startup() -> None:
        startup_waiting.set()
        await release_startup.wait()

    startup_task = asyncio.create_task(wait_for_startup())
    await startup_waiting.wait()

    check_health = AsyncMock(return_value=True)
    response = MagicMock(status_code=200, text="")
    response.json.return_value = {"success": True, "value": "ready"}
    client = AsyncMock(spec=httpx.AsyncClient)
    client.request.return_value = response
    deps = _proxy_dependencies(check_health=check_health, client=client)
    proxy = DaemonProxy(60887, deps_factory=lambda: deps, startup_task=startup_task)

    call_task = asyncio.create_task(proxy.call_tool("example", "ready"))
    await asyncio.sleep(0)

    assert not call_task.done()
    check_health.assert_not_awaited()
    client.request.assert_not_awaited()

    release_startup.set()
    assert await call_task == {"success": True, "value": "ready"}
    check_health.assert_awaited_once()
    client.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_call_after_startup_failure_returns_daemon_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOBBY_AGENT_RUN_ID", raising=False)
    startup_deps = DaemonStartupDependencies(
        bootstrap=BootstrapConfig(daemon_port=60887, websocket_port=60888),
        is_daemon_running=lambda: False,
        get_daemon_pid=lambda: None,
        check_daemon_http_health=AsyncMock(return_value=False),
        start_daemon_process=AsyncMock(return_value={"success": False, "error": "boom"}),
        logger=logging.getLogger(__name__),
    )
    startup_task = asyncio.create_task(ensure_stdio_daemon_running(deps=startup_deps))
    await startup_task

    check_health = AsyncMock(return_value=False)
    client = AsyncMock(spec=httpx.AsyncClient)
    deps = _proxy_dependencies(check_health=check_health, client=client)
    proxy = DaemonProxy(60887, deps_factory=lambda: deps, startup_task=startup_task)

    result = await proxy.call_tool("example", "unavailable")

    assert result["success"] is False
    assert result["error_code"] == "DAEMON_UNAVAILABLE"
    assert "gobby restart --verbose" in result["error"]
    client.request.assert_not_awaited()
