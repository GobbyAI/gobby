"""MCPServer construction for the stdio proxy."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from mcp.server.mcpserver import MCPServer
from mcp.types import Tool

from gobby.config.bootstrap import BootstrapConfig
from gobby.config.bootstrap import load_bootstrap as _load_bootstrap
from gobby.mcp_proxy.instructions import build_gobby_instructions as _build_gobby_instructions
from gobby.mcp_proxy.stdio_proxy import DaemonProxy
from gobby.mcp_proxy.stdio_results import _strip_none
from gobby.mcp_proxy.stdio_tools import register_proxy_tools as _register_proxy_tools
from gobby.utils.daemon_url import resolve_daemon_url
from gobby.utils.version import get_version

logger = logging.getLogger(__name__)


class ProxyFactory(Protocol):
    def __call__(
        self,
        port: int,
        *,
        base_url: str | None = None,
        startup_task: asyncio.Task[None] | None = None,
    ) -> DaemonProxy: ...


class RegisterProxyTools(Protocol):
    def __call__(self, mcp: MCPServer, proxy: DaemonProxy) -> None: ...


class McpServerFactory(Protocol):
    def __call__(
        self,
        name: str,
        *,
        instructions: str,
        version: str,
        lifespan: Callable[[MCPServer[None]], AbstractAsyncContextManager[None]],
    ) -> MCPServer: ...


class _StdioMCPServer(MCPServer[None]):
    """MCPServer whose advertised tool schemas carry no ``null`` fields.

    The SDK's ``exclude_none`` only covers Pydantic model fields; raw
    ``inputSchema`` dicts pass through unchanged, and ``null`` entries break
    strict Jinja prompt templates (e.g. Nemotron Super in LMStudio).
    """

    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        for tool in tools:
            tool.input_schema = _strip_none(tool.input_schema)
        return tools


@dataclass(frozen=True, slots=True)
class StdioServerDependencies:
    load_bootstrap: Callable[[], BootstrapConfig]
    build_gobby_instructions: Callable[[], str]
    mcp_server_factory: McpServerFactory
    proxy_factory: ProxyFactory
    register_proxy_tools: RegisterProxyTools


def default_stdio_server_dependencies() -> StdioServerDependencies:
    return StdioServerDependencies(
        load_bootstrap=lambda: _load_bootstrap(resolve_database_url=False),
        build_gobby_instructions=_build_gobby_instructions,
        mcp_server_factory=_StdioMCPServer,
        proxy_factory=DaemonProxy,
        register_proxy_tools=_register_proxy_tools,
    )


def create_stdio_mcp_server(
    *,
    deps: StdioServerDependencies | None = None,
    startup_task: asyncio.Task[None] | None = None,
) -> MCPServer:
    """Create stdio MCP server without any daemon or hub round trip.

    Construction reads only process-local bootstrap facts. Every proxy tool
    executes against the daemon's HTTP control plane, so the bridge needs no
    hub configuration and no internal registries of its own; resolving them
    here put a PostgreSQL connect, pool open, and config query in front of the
    MCP ``initialize`` handshake, and a loaded control plane stalled that wait
    past the client's startup budget (#22032).
    """
    effective_deps = deps or default_stdio_server_dependencies()
    # The dial port is a pre-database bootstrap fact; the DB-backed config
    # projection carries only the default port and must not decide it.
    bootstrap = effective_deps.load_bootstrap()
    dial_url = resolve_daemon_url(bootstrap=bootstrap)

    if startup_task is None:
        proxy = effective_deps.proxy_factory(bootstrap.daemon_port, base_url=dial_url)
    else:
        proxy = effective_deps.proxy_factory(
            bootstrap.daemon_port,
            base_url=dial_url,
            startup_task=startup_task,
        )

    @asynccontextmanager
    async def proxy_lifespan(_server: MCPServer[None]) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await proxy.aclose()

    mcp = effective_deps.mcp_server_factory(
        "gobby",
        instructions=effective_deps.build_gobby_instructions(),
        version=get_version(),
        lifespan=proxy_lifespan,
    )

    effective_deps.register_proxy_tools(mcp, proxy)
    return mcp
