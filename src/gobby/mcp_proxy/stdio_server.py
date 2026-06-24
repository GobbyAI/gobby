"""FastMCP server construction for the stdio proxy."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP

from gobby.config.app import load_config as _load_config
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
        memory_manager: Any,
    ) -> Any: ...


class ProxyFactory(Protocol):
    def __call__(self, port: int) -> DaemonProxy: ...


class RegisterProxyTools(Protocol):
    def __call__(self, mcp: FastMCP, proxy: DaemonProxy) -> None: ...


class FastMcpFactory(Protocol):
    def __call__(self, name: str, *, instructions: str) -> FastMCP: ...


@dataclass(frozen=True, slots=True)
class StdioServerDependencies:
    load_config: Callable[[], Any]
    setup_internal_registries: SetupInternalRegistries
    build_gobby_instructions: Callable[[], str]
    fast_mcp_factory: FastMcpFactory
    proxy_factory: ProxyFactory
    register_proxy_tools: RegisterProxyTools


def default_stdio_server_dependencies() -> StdioServerDependencies:
    return StdioServerDependencies(
        load_config=_load_config,
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
    config = effective_deps.load_config()

    session_manager = None
    memory_manager = None
    _ = effective_deps.setup_internal_registries(
        _config=config,
        session_manager=session_manager,
        memory_manager=memory_manager,
    )

    mcp = effective_deps.fast_mcp_factory(
        "gobby",
        instructions=effective_deps.build_gobby_instructions(),
    )
    proxy = effective_deps.proxy_factory(config.daemon_port)

    effective_deps.register_proxy_tools(mcp, proxy)

    for tool in _iter_fastmcp_tools(mcp):
        if tool.parameters:
            tool.parameters = _strip_none(tool.parameters)

    return mcp
