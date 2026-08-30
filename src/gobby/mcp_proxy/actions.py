"""
MCP actions for local-first daemon.

Provides simplified MCP server management without platform sync.
"""

import logging
from typing import Any

from gobby.mcp_proxy.manager import MCPClientManager, MCPServerConfig
from gobby.utils.tool_summarizer import generate_server_description

logger = logging.getLogger(__name__)


async def add_mcp_server(
    mcp_manager: MCPClientManager,
    name: str,
    transport: str,
    project_id: str,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    enabled: bool = True,
    description: str | None = None,
    config: MCPServerConfig | None = None,
    template_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Dynamically add a new MCP server connection.

    Args:
        mcp_manager: MCP client manager instance
        name: Unique server name
        transport: Transport type (http, stdio, websocket)
        project_id: Required project ID - all servers must belong to a project
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
        name = name.lower()
        if config is None:
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
                project_id=project_id,
            )
        else:
            name = config.name
            description = config.description if description is None else description
            if template_values is not None:
                config.template_values = template_values

        result = await mcp_manager.add_server(config)

        if not result.get("success"):
            return result

        full_tool_schemas = result.get("full_tool_schemas", [])
        skip_generated_description = bool(config.template and config.description)

        if not description and full_tool_schemas and not skip_generated_description:
            try:
                description = await generate_server_description(
                    server_name=name, tool_summaries=full_tool_schemas
                )
                await mcp_manager.set_server_description(name, description)
            except Exception as e:
                logger.warning("Failed to generate server description: %s", e)
                description = None

        if description is not None:
            result["description"] = description

        logger.debug("Added MCP server: %s (%s)", name, transport)
        return result

    except Exception as e:
        logger.error("Failed to add MCP server '%s': %s", name, e)
        return {
            "success": False,
            "name": name,
            "error": str(e),
            "message": f"Failed to add server: {e}",
        }


async def remove_mcp_server(
    mcp_manager: MCPClientManager,
    name: str,
    project_id: str,
) -> dict[str, Any]:
    """
    Remove an MCP server.

    Args:
        mcp_manager: MCP client manager instance
        name: Server name to remove
        project_id: Required project ID

    Returns:
        Result dict with success status
    """
    try:
        result = await mcp_manager.remove_server(name, project_id=project_id)
        if result.get("success"):
            logger.debug("Removed MCP server: %s (project %s)", name, project_id)
        return result

    except Exception as e:
        logger.error("Failed to remove MCP server '%s': %s", name, e)
        return {
            "success": False,
            "name": name,
            "error": str(e),
            "message": f"Failed to remove server: {e}",
        }
