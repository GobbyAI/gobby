"""FastMCP server construction for the stdio proxy."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP

from gobby.cli.runtime import CliRuntime
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.bootstrap import load_bootstrap as _load_bootstrap
from gobby.mcp_proxy.instructions import build_gobby_instructions as _build_gobby_instructions
from gobby.mcp_proxy.registries import setup_internal_registries as _setup_internal_registries
from gobby.mcp_proxy.stdio_proxy import DaemonProxy
from gobby.mcp_proxy.stdio_results import _strip_none
from gobby.mcp_proxy.stdio_tools import register_proxy_tools as _register_proxy_tools

logger = logging.getLogger(__name__)


class SetupInternalRegistries(Protocol):
    def __call__(
        self,
        *,
        _config: Any,
        session_manager: Any,
        memory_manager_resolver: Any,
    ) -> Any: ...


class ProxyFactory(Protocol):
    def __call__(self, port: int) -> DaemonProxy: ...


class RegisterProxyTools(Protocol):
    def __call__(self, mcp: FastMCP, proxy: DaemonProxy) -> None: ...


class FastMcpFactory(Protocol):
    def __call__(
        self,
        name: str,
        *,
        instructions: str,
        lifespan: Callable[[FastMCP], AbstractAsyncContextManager[None]],
    ) -> FastMCP: ...


@dataclass(frozen=True, slots=True)
class StdioServerDependencies:
    runtime_factory: Callable[[], CliRuntime]
    load_bootstrap: Callable[[], BootstrapConfig]
    setup_internal_registries: SetupInternalRegistries
    build_gobby_instructions: Callable[[], str]
    fast_mcp_factory: FastMcpFactory
    proxy_factory: ProxyFactory
    register_proxy_tools: RegisterProxyTools


def default_stdio_server_dependencies() -> StdioServerDependencies:
    return StdioServerDependencies(
        runtime_factory=lambda: CliRuntime(None),
        load_bootstrap=lambda: _load_bootstrap(resolve_database_url=False),
        setup_internal_registries=_setup_internal_registries,
        build_gobby_instructions=_build_gobby_instructions,
        fast_mcp_factory=FastMCP,
        proxy_factory=DaemonProxy,
        register_proxy_tools=_register_proxy_tools,
    )


def _iter_fastmcp_tools(mcp: FastMCP) -> list[Any]:
    tool_manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(tool_manager, "_tools", None)
    if not isinstance(tools, dict):
        logger.warning("FastMCP private tool registry is unavailable; parameters not normalized")
        return []
    return list(tools.values())


def create_stdio_mcp_server(
    *,
    deps: StdioServerDependencies | None = None,
) -> FastMCP:
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
        _config=config,
        session_manager=session_manager,
        memory_manager_resolver=None,
    )

    proxy = effective_deps.proxy_factory(bootstrap.daemon_port)

    @asynccontextmanager
    async def proxy_lifespan(_server: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await proxy.aclose()

    mcp = effective_deps.fast_mcp_factory(
        "gobby",
        instructions=effective_deps.build_gobby_instructions(),
        lifespan=proxy_lifespan,
    )

    effective_deps.register_proxy_tools(mcp, proxy)

    for tool in _iter_fastmcp_tools(mcp):
        if tool.parameters:
            tool.parameters = _strip_none(tool.parameters)

    return mcp
