"""Resource read operations for the tool proxy service."""

from typing import Any


async def read_resource(service: Any, server_name: str, uri: str) -> Any:
    """Read a resource from an MCP server."""
    return await service._mcp_manager.read_resource(server_name, uri)


__all__ = ["read_resource"]
