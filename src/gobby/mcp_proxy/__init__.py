"""
MCP (Model Context Protocol) package for gobby daemon.

This package provides:
- MCPClientManager: Multi-server connection management
- MCPServerConfig: Server configuration dataclass
- MCP actions: add/remove/list servers
- create_mcp_server: FastMCP server factory
"""

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
