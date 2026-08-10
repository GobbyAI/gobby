from __future__ import annotations

from dataclasses import fields
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gobby.cli.runtime import CliRuntime
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.mcp_proxy.stdio_daemon import DaemonStartupDependencies
from gobby.mcp_proxy.stdio_daemon import ensure_daemon_running as ensure_stdio_daemon_running
from gobby.mcp_proxy.stdio_proxy import DaemonProxy, DaemonProxyDependencies
from gobby.mcp_proxy.stdio_results import DAEMON_HEALTH_CHECK_TIMEOUT_SECONDS
from gobby.mcp_proxy.stdio_server import (
    StdioServerDependencies,
    create_stdio_mcp_server,
)


def test_stdio_dependencies_use_runtime_access() -> None:
    startup_fields = {field.name for field in fields(DaemonStartupDependencies)}
    proxy_fields = {field.name for field in fields(DaemonProxyDependencies)}
    server_fields = {field.name for field in fields(StdioServerDependencies)}

    assert "load_config" not in startup_fields | proxy_fields | server_fields
    assert "bootstrap" in startup_fields
    assert "runtime_factory" in proxy_fields
    assert "runtime_factory" in server_fields


@pytest.mark.asyncio
async def test_stdio_daemon_config_boundary() -> None:
    start_calls: list[tuple[int, int]] = []
    health_calls: list[tuple[int, float, str | None]] = []

    async def start_daemon(port: int, websocket_port: int) -> dict[str, object]:
        start_calls.append((port, websocket_port))
        return {"success": True}

    async def check_health(
        port: int,
        timeout: float = 5.0,
        *,
        base_url: str | None = None,
    ) -> bool:
        health_calls.append((port, timeout, base_url))
        return True

    bootstrap = BootstrapConfig(
        daemon_port=61031,
        websocket_port=61032,
        daemon_url=None,
    )
    deps = DaemonStartupDependencies(
        bootstrap=bootstrap,
        is_daemon_running=lambda: False,
        check_daemon_http_health=check_health,
        start_daemon_process=start_daemon,
        get_daemon_pid=lambda: None,
        logger=MagicMock(),
    )

    await ensure_stdio_daemon_running(deps=deps)

    assert start_calls == [(61031, 61032)]
    assert health_calls == [(61031, DAEMON_HEALTH_CHECK_TIMEOUT_SECONDS, "http://127.0.0.1:61031")]


@pytest.mark.asyncio
async def test_stdio_operation_reads_one_revision() -> None:
    first = DaemonConfig.model_validate(
        {
            "daemon_port": 61041,
            "mcp_client_proxy": {"tool_timeouts": {"custom_tool": 12.0}},
        }
    )
    second = DaemonConfig.model_validate(
        {
            "daemon_port": 61042,
            "mcp_client_proxy": {"tool_timeouts": {"custom_tool": 24.0}},
        }
    )
    proxy_runtime_factory = MagicMock(
        side_effect=(CliRuntime(None, first), CliRuntime(None, second))
    )
    proxy_deps = DaemonProxyDependencies(
        runtime_factory=proxy_runtime_factory,
        check_daemon_http_health=AsyncMock(return_value=True),
        read_project_id=lambda: None,
        http_client_factory=httpx.AsyncClient,
        logger=MagicMock(),
    )
    proxy = DaemonProxy(61041, deps_factory=lambda: proxy_deps)
    request = AsyncMock(return_value={"success": True})

    with patch.object(proxy, "_request", new=request):
        result = await proxy.call_tool("gobby-tasks", "custom_tool")

    assert result == {"success": True}
    proxy_runtime_factory.assert_called_once_with()
    request.assert_awaited_once_with(
        "POST",
        "/api/mcp/gobby-tasks/tools/custom_tool",
        json={},
        timeout=12.0,
        preflight=True,
    )

    server_runtime_factory = MagicMock(
        side_effect=(CliRuntime(None, first), CliRuntime(None, second))
    )
    setup_registries = MagicMock()
    proxy_factory = MagicMock(return_value=proxy)
    mcp_server = MagicMock()
    server_deps = StdioServerDependencies(
        runtime_factory=server_runtime_factory,
        setup_internal_registries=setup_registries,
        build_gobby_instructions=lambda: "instructions",
        fast_mcp_factory=MagicMock(return_value=mcp_server),
        proxy_factory=proxy_factory,
        register_proxy_tools=MagicMock(),
    )

    server = create_stdio_mcp_server(deps=server_deps)

    assert server is mcp_server
    server_runtime_factory.assert_called_once_with()
    setup_registries.assert_called_once_with(
        _config=first,
        session_manager=None,
        memory_manager=None,
    )
    proxy_factory.assert_called_once_with(61041)
