"""FastAPI dependency injection functions for MCP routes.

These dependencies extract server components from app.state, enabling:
- Proper testability (dependencies can be mocked/overridden)
- Clear dependency graph
- Natural code splitting
- Standard FastAPI conventions
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.llm import LLMService
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.mcp_proxy.metrics import ToolMetricsManager
    from gobby.mcp_proxy.tools.internal import InternalRegistryManager
    from gobby.servers.http import HTTPServer
    from gobby.storage.mcp import LocalMCPManager

__all__ = [
    "get_server",
    "get_mcp_manager",
    "get_mcp_manager_required",
    "get_internal_manager",
    "get_tools_handler",
    "get_config",
    "get_mcp_db_manager",
    "get_llm_service",
    "get_metrics_manager",
    "resolve_project_id",
]


async def get_server(request: Request) -> HTTPServer:
    """Get the HTTPServer instance from app state."""
    server = getattr(request.app.state, "server", None)
    if server is None:
        raise HTTPException(status_code=503, detail="Server not initialized")
    # Import here to avoid circular import, cast to help mypy
    from gobby.servers.http import HTTPServer

    return cast(HTTPServer, server)


async def get_mcp_manager(request: Request) -> MCPClientManager | None:
    """Get the MCP client manager for external MCP servers."""
    server = await get_server(request)
    return server.mcp_manager


async def get_mcp_manager_required(request: Request) -> MCPClientManager:
    """Get the MCP client manager, raising if unavailable."""
    manager = await get_mcp_manager(request)
    if manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not available")
    return manager


async def get_internal_manager(request: Request) -> InternalRegistryManager | None:
    """Get the internal tool registry manager (gobby-tasks, gobby-memory, etc.)."""
    server = await get_server(request)
    return server._internal_manager


async def get_tools_handler(request: Request) -> Any:
    """Get the tools handler for Gobby daemon tools."""
    server = await get_server(request)
    return server._tools_handler


async def get_config(request: Request) -> DaemonConfig | None:
    """Get the application configuration."""
    server = await get_server(request)
    return server.config


async def get_mcp_db_manager(request: Request) -> LocalMCPManager | None:
    """Get the MCP database manager."""
    server = await get_server(request)
    return cast(LocalMCPManager | None, server._mcp_db_manager)


async def get_llm_service(request: Request) -> LLMService | None:
    """Get the LLM service for AI-powered operations."""
    server = await get_server(request)
    return server.llm_service


async def resolve_project_id(request: Request, project_id: str | None = None) -> str:
    """
    Resolve a project ID, defaulting to the current project if not specified.

    Args:
        request: FastAPI request object
        project_id: Optional explicit project ID

    Returns:
        Resolved project ID

    Raises:
        HTTPException: If no project ID can be resolved
    """
    server = await get_server(request)
    resolved = server.resolve_project_id(project_id, cwd=None)
    if resolved is None:
        raise HTTPException(status_code=400, detail="No project ID provided or detected")
    return resolved


async def get_metrics_manager(request: Request) -> ToolMetricsManager | None:
    """Get the tool metrics manager for tracking tool call statistics."""
    server = await get_server(request)
    return server.metrics_manager
