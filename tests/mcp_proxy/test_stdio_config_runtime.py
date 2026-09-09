from __future__ import annotations

import asyncio
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


@pytest.fixture(autouse=True)
def _isolate_bridge_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these tests independent of the surrounding process env.

    A managed agent runs with ``GOBBY_AGENT_RUN_ID`` and ``GOBBY_DAEMON_URL``
    set, which suppresses daemon auto-start and retargets the dial URL; tests
    that assert either failed only when run from inside one. Tests needing these
    variables set them explicitly, which still wins over this fixture.
    """
    for name in ("GOBBY_AGENT_RUN_ID", "GOBBY_DAEMON_URL", "GOBBY_PORT", "GOBBY_DAEMON_PORT"):
        monkeypatch.delenv(name, raising=False)


def test_stdio_dependencies_use_runtime_access() -> None:
    startup_fields = {field.name for field in fields(DaemonStartupDependencies)}
    proxy_fields = {field.name for field in fields(DaemonProxyDependencies)}
    server_fields = {field.name for field in fields(StdioServerDependencies)}

    assert "load_config" not in startup_fields | proxy_fields | server_fields
    assert "bootstrap" in startup_fields
    assert "runtime_factory" in proxy_fields
    assert "load_bootstrap" in server_fields
    # Server construction owns no hub-facing dependency, so it cannot block the
    # MCP initialize handshake on the control plane (#22032). The proxy keeps
    # its runtime factory: that read is deferred to the first tool call.
    assert "runtime_factory" not in server_fields
    assert "setup_internal_registries" not in server_fields


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_url", "expected_port", "expected_start"),
    [
        (None, 61031, True),
        ("http://127.0.0.1:31579", 31579, True),
        ("https://daemon.example:31415", 31415, False),
    ],
)
async def test_stdio_daemon_config_boundary(
    monkeypatch: pytest.MonkeyPatch,
    runtime_url: str | None,
    expected_port: int,
    expected_start: bool,
) -> None:
    if runtime_url:
        monkeypatch.setenv("GOBBY_DAEMON_URL", runtime_url)
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

    assert start_calls == ([(expected_port, 61032)] if expected_start else [])
    assert health_calls == [
        (
            expected_port,
            DAEMON_HEALTH_CHECK_TIMEOUT_SECONDS,
            runtime_url or "http://127.0.0.1:61031",
        )
    ]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_url", [None, "http://127.0.0.1:31579"])
@pytest.mark.parametrize("with_startup_task", [False, True])
async def test_stdio_server_takes_dial_port_from_bootstrap(
    monkeypatch: pytest.MonkeyPatch, runtime_url: str | None, with_startup_task: bool
) -> None:
    if runtime_url:
        monkeypatch.setenv("GOBBY_DAEMON_URL", runtime_url)
    proxy = MagicMock()
    proxy_factory = MagicMock(return_value=proxy)
    mcp_server = MagicMock()
    mcp_server_factory = MagicMock(return_value=mcp_server)
    register_proxy_tools = MagicMock()
    server_deps = StdioServerDependencies(
        load_bootstrap=lambda: BootstrapConfig(daemon_port=61031),
        build_gobby_instructions=lambda: "instructions",
        mcp_server_factory=mcp_server_factory,
        proxy_factory=proxy_factory,
        register_proxy_tools=register_proxy_tools,
    )

    startup_task = asyncio.create_task(asyncio.sleep(0)) if with_startup_task else None
    try:
        server = create_stdio_mcp_server(deps=server_deps, startup_task=startup_task)
    finally:
        if startup_task is not None:
            await startup_task

    assert server is mcp_server
    # The explicit managed URL wins when the sandbox hides bootstrap.yaml.
    expected_url = runtime_url or "http://127.0.0.1:61031"
    if startup_task is None:
        proxy_factory.assert_called_once_with(61031, base_url=expected_url)
    else:
        proxy_factory.assert_called_once_with(
            61031, base_url=expected_url, startup_task=startup_task
        )
    # The whole server is wired from bootstrap facts alone: no hub read stands
    # between process start and the MCP initialize handshake (#22032).
    server_kwargs = mcp_server_factory.call_args.kwargs
    assert mcp_server_factory.call_args.args == ("gobby",)
    assert server_kwargs["instructions"] == "instructions"
    assert callable(server_kwargs["lifespan"])
    register_proxy_tools.assert_called_once_with(mcp_server, proxy)
