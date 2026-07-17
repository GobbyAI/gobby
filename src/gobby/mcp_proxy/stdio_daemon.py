"""Daemon startup helpers for the stdio MCP wrapper."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP

from gobby.config.app import load_config as _load_config
from gobby.config.bootstrap import DEFAULT_DAEMON_PORT, DEFAULT_WEBSOCKET_PORT
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
    def __call__(self) -> FastMCP: ...


@dataclass(frozen=True, slots=True)
class DaemonStartupDependencies:
    load_config: Callable[[], Any]
    is_daemon_running: Callable[[], bool]
    check_daemon_http_health: CheckDaemonHealth
    start_daemon_process: StartDaemonProcess
    get_daemon_pid: Callable[[], int | None]
    logger: logging.Logger


def default_daemon_startup_dependencies() -> DaemonStartupDependencies:
    return DaemonStartupDependencies(
        load_config=_load_config,
        is_daemon_running=_is_daemon_running,
        check_daemon_http_health=_check_daemon_http_health,
        start_daemon_process=_start_daemon_process,
        get_daemon_pid=_get_daemon_pid,
        logger=logging.getLogger("gobby.mcp.stdio"),
    )


_LOCAL_DAEMON_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _coerce_port(value: Any, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


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
    config = effective_deps.load_config()
    daemon_port = _coerce_port(getattr(config, "daemon_port", None), DEFAULT_DAEMON_PORT)
    configured_daemon_url = getattr(config, "daemon_url", None)
    dial_url, port, is_local_dial_target = _resolved_dial_target(
        daemon_port,
        configured_daemon_url if isinstance(configured_daemon_url, str) else None,
    )
    ws_port = _coerce_port(
        getattr(getattr(config, "websocket", None), "port", None),
        DEFAULT_WEBSOCKET_PORT,
    )

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
        for attempt in range(DAEMON_HEALTH_ATTEMPTS):
            if await effective_deps.check_daemon_http_health(
                port,
                timeout=DAEMON_HEALTH_CHECK_TIMEOUT_SECONDS,
                base_url=dial_url,
            ):
                return
            if attempt < DAEMON_HEALTH_ATTEMPTS - 1:
                effective_deps.logger.warning(
                    "Daemon health check failed (attempt %s/%s), retrying in %.1fs...",
                    attempt + 1,
                    DAEMON_HEALTH_ATTEMPTS,
                    DAEMON_HEALTH_RETRY_DELAY_SECONDS,
                )
                await asyncio.sleep(DAEMON_HEALTH_RETRY_DELAY_SECONDS)

        pid = effective_deps.get_daemon_pid()
        effective_deps.logger.error(
            "Daemon is running but did not become healthy (pid=%s, port=%s) after %s attempts. Refusing to restart it from a stdio MCP client because that can interrupt active dispatch agents.",
            pid,
            port,
            DAEMON_HEALTH_ATTEMPTS,
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
        "Daemon failed to become healthy after %s attempts (pid=%s, port=%s, ws_port=%s, last_health=%s)",
        DAEMON_HEALTH_ATTEMPTS,
        pid,
        port,
        ws_port,
        last_health_response,
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
