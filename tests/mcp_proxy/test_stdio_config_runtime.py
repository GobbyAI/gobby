from __future__ import annotations

from dataclasses import fields
from unittest.mock import AsyncMock, MagicMock, call, patch

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
    assert "load_bootstrap" in server_fields


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
async def test_stdio_proxy_caches_tool_timeouts_for_proxy_lifetime() -> None:
    first = DaemonConfig.model_validate(
        {"mcp_client_proxy": {"tool_timeouts": {"custom_tool": 12.0}}}
    )
    second = DaemonConfig.model_validate(
        {"mcp_client_proxy": {"tool_timeouts": {"custom_tool": 24.0}}}
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
        results = [
            await proxy.call_tool("gobby-tasks", "custom_tool"),
            await proxy.call_tool("gobby-tasks", "custom_tool"),
        ]

    assert results == [{"success": True}, {"success": True}]
    # One config read per proxy lifetime: the second call reuses the cached
    # timeout map instead of opening another hub connection.
    proxy_runtime_factory.assert_called_once_with()
    expected_request = call(
        "POST",
        "/api/mcp/gobby-tasks/tools/custom_tool",
        json={},
        timeout=12.0,
        preflight=True,
    )
    assert request.await_args_list == [expected_request, expected_request]


@pytest.mark.asyncio
async def test_stdio_proxy_retries_timeout_read_after_failure() -> None:
    """A failed timeout read is not cached: the hub may come up later (#20073)."""
    logger = MagicMock()
    runtime = MagicMock()
    runtime.require_config.side_effect = RuntimeError("hub is down")
    proxy_runtime_factory = MagicMock(return_value=runtime)
    proxy_deps = DaemonProxyDependencies(
        runtime_factory=proxy_runtime_factory,
        check_daemon_http_health=AsyncMock(return_value=True),
        read_project_id=lambda: None,
        http_client_factory=httpx.AsyncClient,
        logger=logger,
    )
    proxy = DaemonProxy(61041, deps_factory=lambda: proxy_deps)
    request = AsyncMock(return_value={"success": True})

    with patch.object(proxy, "_request", new=request):
        first = await proxy.call_tool("gobby-tasks", "custom_tool")
        second = await proxy.call_tool("gobby-tasks", "custom_tool")

    assert first == {"success": True}
    assert second == {"success": True}
    assert proxy_runtime_factory.call_count == 2
    assert runtime.close.call_count == 2
    assert request.await_count == 2
    assert all(item.kwargs["timeout"] == 30.0 for item in request.await_args_list)


def test_stdio_server_takes_dial_port_from_bootstrap() -> None:
    config = DaemonConfig.model_validate({"daemon_port": 61041})
    runtime_factory = MagicMock(return_value=CliRuntime(None, config))
    setup_registries = MagicMock()
    proxy = MagicMock()
    proxy_factory = MagicMock(return_value=proxy)
    mcp_server = MagicMock()
    server_deps = StdioServerDependencies(
        runtime_factory=runtime_factory,
        load_bootstrap=lambda: BootstrapConfig(daemon_port=61031),
        setup_internal_registries=setup_registries,
        build_gobby_instructions=lambda: "instructions",
        mcp_server_factory=MagicMock(return_value=mcp_server),
        proxy_factory=proxy_factory,
        register_proxy_tools=MagicMock(),
    )

    server = create_stdio_mcp_server(deps=server_deps)

    assert server is mcp_server
    runtime_factory.assert_called_once_with()
    setup_registries.assert_called_once()
    registry_kwargs = setup_registries.call_args.kwargs
    assert registry_kwargs["config_resolver"]() is config
    assert registry_kwargs["session_manager"] is None
    assert registry_kwargs["memory_manager_resolver"] is None
    # The dial port comes from bootstrap.yaml, never the DB-backed projection.
    proxy_factory.assert_called_once_with(61031)


def test_stdio_server_starts_when_hub_is_down() -> None:
    runtime = MagicMock()
    runtime.require_config.side_effect = RuntimeError("hub is down")
    setup_registries = MagicMock()
    proxy_factory = MagicMock(return_value=MagicMock())
    mcp_server = MagicMock()
    server_deps = StdioServerDependencies(
        runtime_factory=MagicMock(return_value=runtime),
        load_bootstrap=lambda: BootstrapConfig(daemon_port=61031),
        setup_internal_registries=setup_registries,
        build_gobby_instructions=lambda: "instructions",
        mcp_server_factory=MagicMock(return_value=mcp_server),
        proxy_factory=proxy_factory,
        register_proxy_tools=MagicMock(),
    )

    server = create_stdio_mcp_server(deps=server_deps)

    assert server is mcp_server
    runtime.close.assert_called_once_with()
    setup_registries.assert_called_once()
    registry_kwargs = setup_registries.call_args.kwargs
    assert registry_kwargs["config_resolver"]() is None
    assert registry_kwargs["session_manager"] is None
    assert registry_kwargs["memory_manager_resolver"] is None
    proxy_factory.assert_called_once_with(61031)
