"""
MCP actions for local-first daemon.

Provides simplified MCP server management without platform sync.
"""

import logging
from typing import Any

from gobby.mcp.manager import MCPClientManager, MCPServerConfig
from gobby.tools.summarizer import generate_server_description

logger = logging.getLogger(__name__)


async def add_mcp_server(
    mcp_manager: MCPClientManager,
    name: str,
    transport: str,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    enabled: bool = True,
    description: str | None = None,
) -> dict[str, Any]:
    """
    Dynamically add a new MCP server connection.

    Args:
        mcp_manager: MCP client manager instance
        name: Unique server name
        transport: Transport type (http, stdio, websocket)
        url: Server URL (for http/websocket)
        headers: Custom HTTP headers
        command: Command to run (for stdio)
        args: Command arguments (for stdio)
        env: Environment variables (for stdio)
        enabled: Whether server is enabled
        description: Optional server description

    Returns:
        Result dict with success status and server info
    """
    try:
        # Normalize server name to lowercase
        name = name.lower()

        # Create configuration
        config = MCPServerConfig(
            name=name,
            transport=transport,
            url=url,
            headers=headers,
            command=command,
            args=args,
            env=env,
            enabled=enabled,
            description=description,
        )

        # Add server via manager (connects and caches tools)
        result = await mcp_manager.add_server(config)

        if not result.get("success"):
            return result

        # Get full tool schemas from add_server result
        full_tool_schemas = result.get("full_tool_schemas", [])

        # Generate server description using AI if not provided
        if not description and full_tool_schemas:
            try:
                server_description = await generate_server_description(
                    server_name=name, tool_summaries=full_tool_schemas
                )
                config.description = server_description
            except Exception as e:
                logger.warning(f"Failed to generate server description: {e}")

        logger.debug(f"Added MCP server: {name} ({transport})")
        return result

    except Exception as e:
        logger.error(f"Failed to add MCP server '{name}': {e}")
        return {
            "success": False,
            "name": name,
            "error": str(e),
            "message": f"Failed to add server: {e}",
        }


async def remove_mcp_server(
    mcp_manager: MCPClientManager,
    name: str,
) -> dict[str, Any]:
    """
    Remove an MCP server.

    Args:
        mcp_manager: MCP client manager instance
        name: Server name to remove

    Returns:
        Result dict with success status
    """
    try:
        result = await mcp_manager.remove_server(name)
        if result.get("success"):
            logger.debug(f"Removed MCP server: {name}")
        return result

    except Exception as e:
        logger.error(f"Failed to remove MCP server '{name}': {e}")
        return {
            "success": False,
            "name": name,
            "error": str(e),
            "message": f"Failed to remove server: {e}",
        }


async def list_mcp_servers(
    mcp_manager: MCPClientManager,
) -> dict[str, Any]:
    """
    List all configured MCP servers.

    Args:
        mcp_manager: MCP client manager instance

    Returns:
        Dict with servers list and status
    """
    try:
        servers = []
        for config in mcp_manager.server_configs:
            health = mcp_manager.health.get(config.name)
            servers.append({
                "name": config.name,
                "transport": config.transport,
                "enabled": config.enabled,
                "url": config.url,
                "command": config.command,
                "description": config.description,
                "connected": config.name in mcp_manager.connections,
                "state": health.state.value if health else "unknown",
                "tools": config.tools or [],
            })

        return {
            "success": True,
            "servers": servers,
            "total_count": len(servers),
            "connected_count": len(mcp_manager.connections),
        }

    except Exception as e:
        logger.error(f"Failed to list MCP servers: {e}")
        return {
            "success": False,
            "error": str(e),
            "servers": [],
        }
