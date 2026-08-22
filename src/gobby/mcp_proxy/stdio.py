"""
Stdio MCP server implementation.

This module is the public compatibility facade for the stdio MCP wrapper.
Implementation lives in focused stdio_* modules.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from mcp.server.mcpserver import MCPServer

from gobby.cli.runtime import CliRuntime
from gobby.config.bootstrap import load_bootstrap
from gobby.mcp_proxy._call_tool_wrapper import (
    CallToolWrapperInputError,
    canonicalize_call_tool_wrapper,
)
from gobby.mcp_proxy.daemon_control import (
    check_daemon_http_health,
    get_daemon_pid,
    is_daemon_running,
    restart_daemon_process,
    start_daemon_process,
    stop_daemon_process,
)
from gobby.mcp_proxy.instructions import build_gobby_instructions
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.mcp_proxy.stdio_daemon import (
    DaemonStartupDependencies,
)
from gobby.mcp_proxy.stdio_daemon import (
    ensure_daemon_running as _ensure_daemon_running,
)
from gobby.mcp_proxy.stdio_daemon import (
    main as _daemon_main,
)
from gobby.mcp_proxy.stdio_proxy import (
    DaemonProxy as _DaemonProxy,
)
from gobby.mcp_proxy.stdio_proxy import (
    DaemonProxyDependencies,
)
from gobby.mcp_proxy.stdio_proxy import (
    read_project_id as _read_project_id,
)
from gobby.mcp_proxy.stdio_results import (
    DAEMON_HEALTH_ATTEMPTS,
    DAEMON_HEALTH_CHECK_TIMEOUT_SECONDS,
    DAEMON_HEALTH_RETRY_DELAY_SECONDS,
    DAEMON_PROXY_PREFLIGHT_CACHE_SECONDS,
    DAEMON_PROXY_PREFLIGHT_TIMEOUT_SECONDS,
    REMOVED_WORKFLOW_WAIT_TOOL,
    _daemon_unavailable_result,
    _removed_wait_for_completion_result,
    _request_timeout_result,
    _strip_none,
)
from gobby.mcp_proxy.stdio_server import (
    StdioServerDependencies,
    _StdioMCPServer,
)
from gobby.mcp_proxy.stdio_server import (
    create_stdio_mcp_server as _create_stdio_mcp_server,
)
from gobby.mcp_proxy.stdio_tools import (
    ToolRegistrationDependencies,
)
from gobby.mcp_proxy.stdio_tools import (
    register_proxy_tools as _register_proxy_tools,
)
from gobby.mcp_proxy.wait_tools import (
    call_with_wait_heartbeat,
    prepare_client_guard,
)

logger = logging.getLogger("gobby.mcp.stdio")

__all__ = [
    "DAEMON_HEALTH_ATTEMPTS",
    "DAEMON_HEALTH_CHECK_TIMEOUT_SECONDS",
    "DAEMON_HEALTH_RETRY_DELAY_SECONDS",
    "DAEMON_PROXY_PREFLIGHT_CACHE_SECONDS",
    "DAEMON_PROXY_PREFLIGHT_TIMEOUT_SECONDS",
    "DaemonProxy",
    "REMOVED_WORKFLOW_WAIT_TOOL",
    "_daemon_unavailable_result",
    "_removed_wait_for_completion_result",
    "_request_timeout_result",
    "_strip_none",
    "call_with_wait_heartbeat",
    "canonicalize_call_tool_wrapper",
    "check_daemon_http_health",
    "create_stdio_mcp_server",
    "ensure_daemon_running",
    "get_daemon_pid",
    "httpx",
    "is_daemon_running",
    "main",
    "prepare_client_guard",
    "register_proxy_tools",
    "restart_daemon_process",
    "setup_internal_registries",
    "start_daemon_process",
    "stop_daemon_process",
    "time",
]


def _proxy_dependencies() -> DaemonProxyDependencies:
    return DaemonProxyDependencies(
        runtime_factory=lambda: CliRuntime(None),
        check_daemon_http_health=check_daemon_http_health,
        read_project_id=_read_project_id,
        http_client_factory=httpx.AsyncClient,
        logger=logger,
    )


class DaemonProxy(_DaemonProxy):
    """Compatibility proxy using dependency names from this facade."""

    def __init__(self, port: int):
        super().__init__(port, deps_factory=_proxy_dependencies)


def _tool_registration_dependencies() -> ToolRegistrationDependencies:
    return ToolRegistrationDependencies(
        canonicalize_call_tool_wrapper=canonicalize_call_tool_wrapper,
        input_error_type=CallToolWrapperInputError,
        prepare_client_guard=prepare_client_guard,
        call_with_wait_heartbeat=call_with_wait_heartbeat,
    )


def register_proxy_tools(mcp: MCPServer, proxy: _DaemonProxy) -> None:
    """Register proxy tools on the MCP server."""
    _register_proxy_tools(mcp, proxy, deps_factory=_tool_registration_dependencies)


def _server_dependencies() -> StdioServerDependencies:
    return StdioServerDependencies(
        runtime_factory=lambda: CliRuntime(None),
        load_bootstrap=lambda: load_bootstrap(resolve_database_url=False),
        setup_internal_registries=setup_internal_registries,
        build_gobby_instructions=build_gobby_instructions,
        mcp_server_factory=_StdioMCPServer,
        proxy_factory=DaemonProxy,
        register_proxy_tools=register_proxy_tools,
    )


def create_stdio_mcp_server() -> MCPServer:
    """Create stdio MCP server."""
    return _create_stdio_mcp_server(deps=_server_dependencies())


def _daemon_dependencies() -> DaemonStartupDependencies:
    return DaemonStartupDependencies(
        bootstrap=load_bootstrap(resolve_database_url=False),
        is_daemon_running=is_daemon_running,
        check_daemon_http_health=check_daemon_http_health,
        start_daemon_process=start_daemon_process,
        get_daemon_pid=get_daemon_pid,
        logger=logger,
    )


async def ensure_daemon_running() -> None:
    """Ensure the Gobby daemon is running and healthy."""
    await _ensure_daemon_running(deps=_daemon_dependencies())


async def main() -> None:
    """Main entry point for stdio MCP server."""
    await _daemon_main(deps=_daemon_dependencies(), create_server=create_stdio_mcp_server)


if __name__ == "__main__":
    asyncio.run(main())
