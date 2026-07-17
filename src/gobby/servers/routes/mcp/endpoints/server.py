"""
Server management endpoints for MCP server lifecycle.

Extracted from tools.py as part of Phase 2 Strangler Fig decomposition.
These endpoints handle server listing, addition, import, and removal.
"""

import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fastapi import Depends, HTTPException, Request

from gobby.mcp_proxy.lazy import CircuitBreakerOpen
from gobby.mcp_proxy.models import MCPError
from gobby.servers.routes.dependencies import get_internal_manager, get_mcp_manager, get_server

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.mcp_proxy.models import MCPServerConfig
    from gobby.mcp_proxy.registry_manager import InternalToolRegistryManager
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


async def _request_json_mapping(request: Request) -> Mapping[str, Any]:
    body = await request.json()
    if not isinstance(body, Mapping):
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": "Request body must be a JSON object"},
        )
    return body


def _mcp_manager_is_connected(mcp_manager: Any, name: str) -> bool:
    is_connected = getattr(mcp_manager, "is_connected", None)
    if callable(is_connected):
        return bool(mcp_manager.is_connected(name))

    connections = getattr(mcp_manager, "connections", None)
    return isinstance(connections, dict) and name in connections


def _current_project_id() -> str | None:
    from gobby.utils.project_context import get_project_context

    try:
        project_ctx = get_project_context()
    except (LookupError, OSError):
        logger.debug("Failed to load current project context", exc_info=True)
        return None
    if not project_ctx:
        return None
    project_id = project_ctx.get("id")
    return project_id if isinstance(project_id, str) and project_id else None


def _body_project_id(body: Mapping[str, Any]) -> str | None:
    project_id = body.get("project_id")
    if isinstance(project_id, str) and project_id:
        return project_id
    return _current_project_id()


def _string_dict(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("env and headers must be JSON objects")
    return {str(key): str(item) for key, item in value.items() if str(key)}


def _public_secret_refs(value: Mapping[str, str] | None) -> dict[str, str] | None:
    """Return only safe secret-reference values for unauthenticated server listings."""
    if not value:
        return None
    refs = {
        str(key): item
        for key, item in value.items()
        if str(key) and isinstance(item, str) and item.startswith("$secret:")
    }
    return refs or None


def _string_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("args must be a JSON array")
    return [str(item) for item in value]


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value)
    return value


def _bool_field(body: Mapping[str, Any], name: str, default: bool) -> bool:
    value = body.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _build_mcp_server_config(
    body: Mapping[str, Any],
    *,
    name: str,
    project_id: str,
) -> "MCPServerConfig":
    from gobby.mcp_proxy.models import MCPServerConfig

    connect_timeout = body.get("connect_timeout", 30.0)
    if connect_timeout is None:
        connect_timeout = 30.0

    config = MCPServerConfig(
        name=name,
        project_id=project_id,
        transport=str(body.get("transport") or "http"),
        url=_optional_string(body.get("url")),
        command=_optional_string(body.get("command")),
        args=_string_list(body.get("args")),
        env=_string_dict(body.get("env")),
        headers=_string_dict(body.get("headers")),
        enabled=_bool_field(body, "enabled", True),
        description=_optional_string(body.get("description")),
        requires_oauth=_bool_field(body, "requires_oauth", False),
        oauth_provider=_optional_string(body.get("oauth_provider")),
        connect_timeout=float(connect_timeout),
    )
    config.validate()
    return config


async def list_mcp_servers(
    internal_manager: "InternalToolRegistryManager | None" = Depends(get_internal_manager),
    mcp_manager: "MCPClientManager | None" = Depends(get_mcp_manager),
) -> dict[str, Any]:
    """
    List all configured MCP servers.

    Args:
        internal_manager: Internal tool registry manager (injected)
        mcp_manager: External MCP client manager (injected)

    Returns:
        List of servers with connection status
    """

    try:
        server_list = []

        # Add internal servers (gobby-tasks, gobby-memory, etc.)
        if internal_manager:
            for registry in internal_manager.get_all_registries():
                server_list.append(
                    {
                        "name": registry.name,
                        "state": "connected",
                        "transport": "internal",
                        "connected": True,
                        "available": True,
                    }
                )

        # Add external MCP servers
        connected_count = len(server_list)  # all internal servers are connected
        if mcp_manager:
            for config in mcp_manager.server_configs:
                health = mcp_manager.health.get(config.name)
                state = health.state.value if health else "unknown"
                is_connected = _mcp_manager_is_connected(mcp_manager, config.name)
                if is_connected:
                    connected_count += 1
                entry: dict[str, Any] = {
                    "name": config.name,
                    "state": state,
                    "transport": config.transport,
                    "connected": is_connected,
                    "available": True,
                    "project_id": config.project_id,
                    "description": config.description,
                    "url": config.url,
                    "command": config.command,
                    "args": config.args,
                    "env": _public_secret_refs(config.env),
                    "headers": _public_secret_refs(config.headers),
                    "enabled": config.enabled,
                    "requires_oauth": config.requires_oauth,
                    "oauth_provider": config.oauth_provider,
                    "connect_timeout": config.connect_timeout,
                }
                server_list.append(entry)

        return {
            "success": True,
            "servers": server_list,
            "total": len(server_list),
            "connected": connected_count,
        }

    except Exception as e:
        logger.error("List MCP servers error: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


async def add_mcp_server(
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Add a new MCP server configuration.

    Request body:
        {
            "name": "my-server",
            "transport": "http",
            "url": "https://...",
            "enabled": true
        }

    Returns:
        Success status
    """
    start_time = time.perf_counter()

    try:
        body = await _request_json_mapping(request)
        name = body.get("name")
        transport = body.get("transport")

        if not name or not transport:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "Required fields: name, transport"},
            )

        if not isinstance(name, str):
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "Required field: name (string)"},
            )

        project_id = _body_project_id(body)
        if not project_id:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "No current project found. Run 'gobby init'."},
            )
        config = _build_mcp_server_config(body, name=name, project_id=project_id)

        if server.mcp_manager is None:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "MCP manager not available",
                "response_time_ms": response_time_ms,
            }

        await server.mcp_manager.add_server(config)

        # Broadcast MCP server added event
        ws = server.services.websocket_server
        if ws:
            try:
                await ws.broadcast_mcp_event("server_added", name)
            except Exception as e:
                logger.debug("Failed to broadcast mcp event server_added: %s", e)

        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {
            "success": True,
            "message": f"Added MCP server: {name}",
            "response_time_ms": response_time_ms,
        }

    except HTTPException:
        raise
    except ValueError as e:
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}
    except Exception as e:
        logger.error("Add MCP server error: %s", e, exc_info=True)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


async def update_mcp_server(
    name: str,
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Update an existing MCP server configuration without renaming it.

    Args:
        name: Existing server registry key

    Returns:
        Success status
    """
    start_time = time.perf_counter()

    try:
        body = await _request_json_mapping(request)
        body_name = body.get("name", name)
        if body_name != name:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "MCP server names cannot be changed"},
            )

        project_id = _body_project_id(body)
        if not project_id:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "No current project found. Run 'gobby init'."},
            )

        config = _build_mcp_server_config(body, name=name, project_id=project_id)

        if server.mcp_manager is None:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "MCP manager not available",
                "response_time_ms": response_time_ms,
            }

        await server.mcp_manager.update_server(name, config, project_id=project_id)

        ws = server.services.websocket_server
        if ws:
            try:
                await ws.broadcast_mcp_event("server_updated", name)
            except Exception as e:
                logger.debug("Failed to broadcast mcp event server_updated: %s", e)

        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {
            "success": True,
            "message": f"Updated MCP server: {name}",
            "response_time_ms": response_time_ms,
        }

    except HTTPException:
        raise
    except ValueError as e:
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}
    except Exception as e:
        logger.error("Update MCP server error: %s", e, exc_info=True)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


async def import_mcp_server(
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Import MCP server(s) from various sources.

    Request body:
        {
            "from_project": "other-project",  # Import from project
            "github_url": "https://...",      # Import from GitHub
            "query": "supabase mcp",          # Search and import
            "servers": ["name1", "name2"]     # Specific servers to import
        }

    Returns:
        Import result with imported/skipped/failed lists
    """
    start_time = time.perf_counter()

    try:
        body = await _request_json_mapping(request)
        from_project = body.get("from_project")
        github_url = body.get("github_url")
        query = body.get("query")
        servers = body.get("servers")

        if not from_project and not github_url and not query:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "error": "Specify at least one: from_project, github_url, or query",
                },
            )

        # Get current project ID from context
        from gobby.utils.project_context import get_project_context

        project_ctx = get_project_context()
        if not project_ctx or not project_ctx.get("id"):
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "No current project. Run 'gobby init' first.",
                "response_time_ms": response_time_ms,
            }
        current_project_id = project_ctx["id"]

        if not server.config:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "Daemon configuration not available",
                "response_time_ms": response_time_ms,
            }

        # Create importer
        from gobby.mcp_proxy.importer import MCPServerImporter

        db = server.services.database
        importer = MCPServerImporter(
            config=server.config,
            db=db,
            current_project_id=current_project_id,
            mcp_client_manager=server.mcp_manager,
            llm_service=getattr(server, "services", None) and server.services.llm_service,
        )

        # Execute import based on source
        # Note: validation above ensures at least one of these is truthy
        if from_project:
            result = await importer.import_from_project(
                source_project=from_project,
                servers=servers,
            )
        elif github_url:
            result = await importer.import_from_github(github_url)
        else:
            # query must be truthy due to earlier validation
            assert query is not None
            result = await importer.import_from_query(query)

        # Broadcast only when an import actually persisted at least one server.
        ws = server.services.websocket_server
        if ws and isinstance(result, dict) and result.get("imported"):
            try:
                await ws.broadcast_mcp_event("server_imported", "bulk")
            except Exception as e:
                logger.debug("Failed to broadcast mcp event server_imported: %s", e)

        response_time_ms = (time.perf_counter() - start_time) * 1000
        if isinstance(result, dict):
            result["response_time_ms"] = response_time_ms
            return result
        return {"success": True, "data": result, "response_time_ms": response_time_ms}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Import MCP server error: %s", e, exc_info=True)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


async def remove_mcp_server(
    name: str,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Remove an MCP server configuration.

    Args:
        name: Server name to remove

    Returns:
        Success status
    """
    start_time = time.perf_counter()

    try:
        if server.mcp_manager is None:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "MCP manager not available",
                "response_time_ms": response_time_ms,
            }

        await server.mcp_manager.remove_server(name, project_id=_current_project_id())

        # Broadcast MCP server removed event
        ws = server.services.websocket_server
        if ws:
            try:
                await ws.broadcast_mcp_event("server_removed", name)
            except Exception as e:
                logger.debug("Failed to broadcast mcp event server_removed: %s", e)

        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {
            "success": True,
            "message": f"Removed MCP server: {name}",
            "response_time_ms": response_time_ms,
        }

    except ValueError as e:
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}
    except Exception as e:
        logger.error("Remove MCP server error: %s", e, exc_info=True)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


async def set_mcp_server_enabled(
    name: str,
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Enable or disable an external MCP server.

    Args:
        name: Server name to update

    Request body:
        {"enabled": true}

    Returns:
        Success status with the resolved enabled state
    """
    start_time = time.perf_counter()

    try:
        body = await _request_json_mapping(request)
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "Required field: enabled (boolean)"},
            )

        if server.mcp_manager is None:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "MCP manager not available",
                "response_time_ms": response_time_ms,
            }

        result = await server.mcp_manager.set_server_enabled(
            name,
            enabled,
            project_id=_current_project_id(),
        )

        # Broadcast MCP server updated event
        ws = server.services.websocket_server
        if ws:
            try:
                await ws.broadcast_mcp_event("server_updated", name)
            except Exception as e:
                logger.debug("Failed to broadcast mcp event server_updated: %s", e)

        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {
            "success": True,
            "name": name,
            "enabled": result.get("enabled", enabled),
            "response_time_ms": response_time_ms,
        }

    except HTTPException:
        raise
    except PermissionError as e:
        logger.warning(
            "Set MCP server enabled permission error",
            extra={"server_name": name, "enabled": enabled, "error": str(e)},
            exc_info=True,
        )
        response_time_ms = (time.perf_counter() - start_time) * 1000
        raise HTTPException(
            status_code=403,
            detail={"success": False, "error": str(e), "response_time_ms": response_time_ms},
        ) from e
    except (ValueError, KeyError, RuntimeError, MCPError, CircuitBreakerOpen) as e:
        logger.error(
            "Set MCP server enabled error",
            extra={"server_name": name, "enabled": enabled, "error": str(e)},
            exc_info=True,
        )
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


__all__ = [
    "list_mcp_servers",
    "add_mcp_server",
    "update_mcp_server",
    "import_mcp_server",
    "remove_mcp_server",
    "set_mcp_server_enabled",
]
