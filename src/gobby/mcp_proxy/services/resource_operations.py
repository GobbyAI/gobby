"""Resource read operations for the tool proxy service."""

from typing import Any

from gobby.mcp_proxy.services.server_resolution import caller_project_id, resolve_server


async def read_resource(
    service: Any,
    server_name: str,
    uri: str,
    *,
    project_id: str | None = None,
    scope: str | None = None,
) -> Any:
    """Read a resource from an MCP server."""
    project_id = caller_project_id(service, project_id=project_id, scope=scope)
    config = resolve_server(service, server_name, project_id=project_id)
    if config is None:
        return {
            "success": False,
            "error": f"Server '{server_name}' not found in project scope {project_id}",
        }
    return await service._mcp_manager.read_resource(config.id, uri)


__all__ = ["read_resource"]
