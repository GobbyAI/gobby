"""Daemon startup helpers for the stdio MCP wrapper."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer

from gobby.config.bootstrap import BootstrapConfig, load_bootstrap
from gobby.mcp_proxy.daemon_control import (
    check_daemon_http_health as _check_daemon_http_health,
)
from gobby.mcp_proxy.daemon_control import get_daemon_pid as _get_daemon_pid
from gobby.mcp_proxy.daemon_control import is_daemon_running as _is_daemon_running
from gobby.mcp_proxy.daemon_control import start_daemon_process as _start_daemon_process
from gobby.mcp_proxy.stdio_results import (
    DAEMON_HEALTH_ATTEMPTS,
    DAEMON_HEALTH_CHECK_TIMEOUT_SECONDS,
    DAEMON_HEALTH_RETRY_DELAY_SECONDS,
)


class CheckDaemonHealth(Protocol):
    def __call__(
        self,
        port: int,
        timeout: float = 5.0,
        *,
        base_url: str | None = None,
    ) -> Awaitable[bool]: ...


class StartDaemonProcess(Protocol):
    def __call__(self, port: int, websocket_port: int) -> Awaitable[dict[str, Any]]: ...


class CreateStdioMcpServer(Protocol):
    def __call__(self) -> MCPServer: ...


@dataclass(frozen=True, slots=True)
class DaemonStartupDependencies:
    bootstrap: BootstrapConfig
    is_daemon_running: Callable[[], bool]
    check_daemon_http_health: CheckDaemonHealth
    start_daemon_process: StartDaemonProcess
    get_daemon_pid: Callable[[], int | None]
    logger: logging.Logger


def default_daemon_startup_dependencies() -> DaemonStartupDependencies:
    return DaemonStartupDependencies(
        bootstrap=load_bootstrap(resolve_database_url=False),
        is_daemon_running=_is_daemon_running,
        check_daemon_http_health=_check_daemon_http_health,
        start_daemon_process=_start_daemon_process,
        get_daemon_pid=_get_daemon_pid,
        logger=logging.getLogger("gobby.mcp.stdio"),
    )


_LOCAL_DAEMON_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _resolved_dial_target(
    default_port: int, resolved_url: str | None = None
) -> tuple[str, int, bool]:
    resolved_url = resolved_url or f"http://127.0.0.1:{default_port}"
    parsed = urlsplit(resolved_url)
    host = parsed.hostname or ""
    port = parsed.port or default_port
    return resolved_url, port, host.lower() in _LOCAL_DAEMON_HOSTS


async def ensure_daemon_running(
    *,
    deps: DaemonStartupDependencies | None = None,
) -> None:
    """Ensure the Gobby daemon is running and healthy."""
    effective_deps = deps or default_daemon_startup_dependencies()
    bootstrap = effective_deps.bootstrap
    dial_url, port, is_local_dial_target = _resolved_dial_target(
        bootstrap.daemon_port,
        bootstrap.daemon_url,
    )
    ws_port = bootstrap.websocket_port

    if not is_local_dial_target:
        if await effective_deps.check_daemon_http_health(
            port,
            timeout=DAEMON_HEALTH_CHECK_TIMEOUT_SECONDS,
            base_url=dial_url,
        ):
            return
        effective_deps.logger.error(
            "Remote Gobby daemon is not healthy at %s; refusing to start a local daemon "
            "for a remote dial target.",
            dial_url,
        )
        return

    if effective_deps.is_daemon_running():
        # Serve stdio immediately: MCP clients budget startup (Codex kills
        # registration at 120s), and a health wait here cannot change the
        # outcome — the daemon is already running, so proxied calls simply
        # fail transiently until it responds. One probe, logging only.
        healthy = await effective_deps.check_daemon_http_health(
            port,
            timeout=DAEMON_HEALTH_CHECK_TIMEOUT_SECONDS,
            base_url=dial_url,
        )
        if not healthy:
            effective_deps.logger.warning(
                "Running daemon did not answer health probe; serving stdio anyway",
                extra={
                    "pid": effective_deps.get_daemon_pid(),
                    "port": port,
                    "ws_port": ws_port,
                },
            )
        return

    if os.environ.get("GOBBY_AGENT_RUN_ID"):
        effective_deps.logger.error(
            "Daemon is not running for managed agent MCP client; refusing to auto-start "
            "from an agent process.",
        )
        return

    result = await effective_deps.start_daemon_process(port, ws_port)
    if not result.get("success"):
        effective_deps.logger.error(
            "Failed to start daemon: %s (port=%s, ws_port=%s)",
            result.get("error", "unknown error"),
            port,
            ws_port,
        )
        return

    last_health_response = None
    for _i in range(DAEMON_HEALTH_ATTEMPTS):
        last_health_response = await effective_deps.check_daemon_http_health(
            port,
            timeout=DAEMON_HEALTH_CHECK_TIMEOUT_SECONDS,
            base_url=dial_url,
        )
        if last_health_response:
            return
        await asyncio.sleep(DAEMON_HEALTH_RETRY_DELAY_SECONDS)

    pid = effective_deps.get_daemon_pid()
    effective_deps.logger.error(
        "Started daemon did not become healthy",
        extra={
            "pid": pid,
            "port": port,
            "ws_port": ws_port,
            "attempts": DAEMON_HEALTH_ATTEMPTS,
            "last_health_response": last_health_response,
        },
    )
    return


async def main(
    *,
    deps: DaemonStartupDependencies | None = None,
    create_server: CreateStdioMcpServer,
) -> None:
    """Main entry point for stdio MCP server."""
    await ensure_daemon_running(deps=deps)
    mcp = create_server()
    await mcp.run_stdio_async()
