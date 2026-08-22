"""MCPServer construction for the stdio proxy."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from mcp.server.mcpserver import MCPServer
from mcp.types import Tool

from gobby.cli.runtime import CliRuntime
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.bootstrap import load_bootstrap as _load_bootstrap
from gobby.mcp_proxy.instructions import build_gobby_instructions as _build_gobby_instructions
from gobby.mcp_proxy.registries import setup_internal_registries as _setup_internal_registries
from gobby.mcp_proxy.stdio_proxy import DaemonProxy
from gobby.mcp_proxy.stdio_results import _strip_none
from gobby.mcp_proxy.stdio_tools import register_proxy_tools as _register_proxy_tools
from gobby.utils.version import get_version

logger = logging.getLogger(__name__)


class SetupInternalRegistries(Protocol):
    def __call__(
        self,
        *,
        config_resolver: Callable[[], Any],
        session_manager: Any,
        memory_manager_resolver: Any,
    ) -> Any: ...


class ProxyFactory(Protocol):
    def __call__(self, port: int) -> DaemonProxy: ...


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
    runtime_factory: Callable[[], CliRuntime]
    load_bootstrap: Callable[[], BootstrapConfig]
    setup_internal_registries: SetupInternalRegistries
    build_gobby_instructions: Callable[[], str]
    mcp_server_factory: McpServerFactory
    proxy_factory: ProxyFactory
    register_proxy_tools: RegisterProxyTools


def default_stdio_server_dependencies() -> StdioServerDependencies:
    return StdioServerDependencies(
        runtime_factory=lambda: CliRuntime(None),
        load_bootstrap=lambda: _load_bootstrap(resolve_database_url=False),
        setup_internal_registries=_setup_internal_registries,
        build_gobby_instructions=_build_gobby_instructions,
        mcp_server_factory=_StdioMCPServer,
        proxy_factory=DaemonProxy,
        register_proxy_tools=_register_proxy_tools,
    )


def create_stdio_mcp_server(
    *,
    deps: StdioServerDependencies | None = None,
) -> MCPServer:
    """Create stdio MCP server."""
    effective_deps = deps or default_stdio_server_dependencies()
    # The dial port is a pre-database bootstrap fact; the DB-backed config
    # projection carries only the default port and must not decide it.
    bootstrap = effective_deps.load_bootstrap()
    runtime = effective_deps.runtime_factory()
    config = None
    try:
        config = runtime.require_config(apply_migrations=False)
    except Exception as exc:
        # Best-effort: lifecycle tools must register even when the hub is
        # down; proxied calls report structured per-call errors instead.
        logger.warning(
            "Hub configuration is unavailable; starting stdio MCP server without it: %s", exc
        )
    finally:
        runtime.close()

    session_manager = None
    _ = effective_deps.setup_internal_registries(
        config_resolver=lambda: config,
        session_manager=session_manager,
        memory_manager_resolver=None,
    )

    proxy = effective_deps.proxy_factory(bootstrap.daemon_port)

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
