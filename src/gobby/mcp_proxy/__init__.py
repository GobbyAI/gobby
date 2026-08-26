"""
MCP (Model Context Protocol) package for gobby daemon.

This package provides:
- MCPClientManager: Multi-server connection management
- MCPServerConfig: Server configuration dataclass
- MCP actions: add/remove/list servers
- create_mcp_server: MCPServer factory
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .manager import (
        ConnectionState,
        HealthState,
        MCPClientManager,
        MCPConnectionHealth,
        MCPError,
        MCPServerConfig,
    )
    from .server import create_mcp_server

__all__ = [
    "ConnectionState",
    "HealthState",
    "MCPClientManager",
    "MCPConnectionHealth",
    "MCPError",
    "MCPServerConfig",
    "create_mcp_server",
]

_EXPORT_MODULES = {
    "ConnectionState": "gobby.mcp_proxy.manager",
    "HealthState": "gobby.mcp_proxy.manager",
    "MCPClientManager": "gobby.mcp_proxy.manager",
    "MCPConnectionHealth": "gobby.mcp_proxy.manager",
    "MCPError": "gobby.mcp_proxy.manager",
    "MCPServerConfig": "gobby.mcp_proxy.manager",
    "create_mcp_server": "gobby.mcp_proxy.server",
}


def __getattr__(name: str) -> Any:
    """Load public proxy exports without initializing the server on package import."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
