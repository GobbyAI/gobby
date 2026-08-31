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

from gobby.mcp_proxy.client_manager.server_registry import visible_configs
from gobby.mcp_proxy.lazy import CircuitBreakerOpen
from gobby.mcp_proxy.models import MCPError, TemplateOwnedFieldsError, TemplateValuesInvalidError
from gobby.mcp_proxy.services.server_mgmt import ServerManagementService
from gobby.mcp_proxy.services.server_resolution import resolve_server
from gobby.servers.routes.dependencies import get_server
from gobby.servers.routes.mcp.endpoints.request_context import request_mcp_scope
from gobby.storage.projects import GLOBAL_PROJECT_ID

if TYPE_CHECKING:
    from gobby.mcp_proxy.models import MCPServerConfig
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


def _mcp_manager_is_connected(mcp_manager: Any, server_id: str) -> bool:
    is_connected = getattr(mcp_manager, "is_connected", None)
    if callable(is_connected):
        return bool(mcp_manager.is_connected(server_id))

    connections = getattr(mcp_manager, "connections", None)
    return isinstance(connections, dict) and server_id in connections


def _scope_label(project_id: str | None) -> str:
    return "global" if project_id == GLOBAL_PROJECT_ID else "project"


def _unknown_server(name: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"success": False, "error": f"Server '{name}' not found"},
    )


def _exact_server(manager: Any, name: str, project_id: str) -> Any:
    db = getattr(manager, "mcp_db_manager", None)
    if db is not None:
        row = db.get_server(name, project_id)
        if row is None:
            raise _unknown_server(name)
        return row
    config = resolve_server(manager, name, project_id=project_id)
    if config is None or str(config.project_id) != project_id:
        raise _unknown_server(name)
    return config


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
    base: "MCPServerConfig | None" = None,
) -> "MCPServerConfig":
    from gobby.mcp_proxy.models import MCPServerConfig

    connect_timeout = body.get("connect_timeout", 30.0 if base is None else base.connect_timeout)
    if connect_timeout is None:
        connect_timeout = 30.0 if base is None else base.connect_timeout

    config = MCPServerConfig(
        name=name,
        project_id=project_id if base is None else base.project_id,
        transport=str(body.get("transport") or (base.transport if base else "http")),
        url=_optional_string(body["url"]) if "url" in body else (base.url if base else None),
        command=_optional_string(body["command"])
        if "command" in body
        else (base.command if base else None),
        args=_string_list(body["args"]) if "args" in body else (base.args if base else None),
        env=_string_dict(body["env"]) if "env" in body else (base.env if base else None),
        headers=_string_dict(body["headers"])
        if "headers" in body
        else (base.headers if base else None),
        enabled=_bool_field(body, "enabled", True if base is None else base.enabled),
        description=_optional_string(body["description"])
        if "description" in body
        else (base.description if base else None),
        requires_oauth=_bool_field(
            body, "requires_oauth", False if base is None else base.requires_oauth
        ),
        oauth_provider=_optional_string(body.get("oauth_provider"))
        if "oauth_provider" in body
        else (base.oauth_provider if base else None),
        connect_timeout=float(connect_timeout),
    )
    if base is not None:
        config.id = base.id
        config.template_id = base.template_id
        config.template = base.template
        config.runtime_hook = base.runtime_hook
        config.template_values = dict(base.template_values or {})
    config.validate()
    return config


async def list_mcp_servers(
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """List MCP servers visible to the resolved project scope."""

    try:
        scope_project = request_mcp_scope(request, server, None)
        server_list = []
        internal_manager = getattr(server, "_internal_manager", None)

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

        connected_count = len(server_list)
        manager = server.mcp_manager
        if manager:
            mapping = getattr(manager, "_configs", None)
            if isinstance(mapping, dict) and mapping:
                configs = visible_configs(manager, scope_project)
            else:
                configs = list(getattr(manager, "server_configs", []) or [])
            for config in configs:
                health = manager.health.get(config.id)
                state = health.state.value if health else "unknown"
                is_connected = _mcp_manager_is_connected(manager, config.id)
                if is_connected:
                    connected_count += 1
                missing = []
                if health is not None and getattr(health, "missing_secrets", None):
                    missing = list(health.missing_secrets or [])
                entry: dict[str, Any] = {
                    "name": config.name,
                    "id": config.id,
                    "state": state,
                    "transport": config.transport,
                    "connected": is_connected,
                    "available": True,
                    "project_id": config.project_id,
                    "scope": _scope_label(config.project_id),
                    "template": config.template,
                    "template_values": _public_secret_refs(config.template_values),
                    "missing_secrets": missing,
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

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("List MCP servers error: %s", e)
        return {"success": False, "error": str(e)}


def _string_values(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("values must be a JSON object")
    return {str(key): str(item) for key, item in value.items() if item is not None}


async def add_mcp_server(
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """Add a server from a template or a manual payload."""
    start_time = time.perf_counter()

    try:
        body = await _request_json_mapping(request)
        name = body.get("name")
        transport = body.get("transport")
        template = body.get("template")

        if not isinstance(name, str) or not name:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "Required field: name (string)"},
            )
        if not template and not transport:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "Required fields: name, transport"},
            )

        if server.mcp_manager is None:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "MCP manager not available",
                "response_time_ms": response_time_ms,
            }

        scope_project = request_mcp_scope(request, server, body)
        scope_label = _scope_label(scope_project)
        service = ServerManagementService(server.mcp_manager, config_manager=server.config)
        result = await service.add_server(
            name,
            str(transport) if isinstance(transport, str) else None,
            url=_optional_string(body.get("url")),
            command=_optional_string(body.get("command")),
            args=_string_list(body.get("args")),
            env=_string_dict(body.get("env")),
            headers=_string_dict(body.get("headers")),
            enabled=_bool_field(body, "enabled", True) if "enabled" in body else True,
            project_id=scope_project,
            template=str(template) if isinstance(template, str) else None,
            values=_string_values(body.get("values")),
            scope=scope_label,
            description=_optional_string(body.get("description")),
        )
        ws = server.services.websocket_server
        if ws and result.get("success"):
            try:
                await ws.broadcast_mcp_event("server_added", name)
            except Exception as e:
                logger.debug("Failed to broadcast mcp event server_added: %s", e)

        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {**result, "response_time_ms": response_time_ms}

    except HTTPException:
        raise
    except ValueError as e:
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}
    except Exception as e:
        logger.exception("Add MCP server error: %s", e)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


async def update_mcp_server(
    name: str,
    request: Request,
    server: "HTTPServer" = Depends(get_server),
    body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Patch a resolved (name, scope) instance by id."""
    start_time = time.perf_counter()

    try:
        if body is None:
            body = await _request_json_mapping(request)
        body_name = body.get("name", name)
        if body_name != name:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "MCP server names cannot be changed"},
            )

        if server.mcp_manager is None:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "MCP manager not available",
                "response_time_ms": response_time_ms,
            }

        scope_project = request_mcp_scope(request, server, body)
        row = _exact_server(server.mcp_manager, name, scope_project)
        server_id = getattr(row, "id", None)
        if not isinstance(server_id, str) or not server_id:
            raise _unknown_server(name)
        result = await server.mcp_manager.update_server(server_id, body, project_id=scope_project)

        ws = server.services.websocket_server
        if ws:
            try:
                await ws.broadcast_mcp_event("server_updated", name)
            except Exception as e:
                logger.debug("Failed to broadcast mcp event server_updated: %s", e)

        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {**result, "response_time_ms": response_time_ms}

    except HTTPException:
        raise
    except TemplateOwnedFieldsError as e:
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": e.error, "fields": e.fields},
        ) from e
    except TemplateValuesInvalidError as e:
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": e.error, "message": str(e)},
        ) from e
    except ValueError as e:
        if "not found" in str(e).lower():
            raise _unknown_server(name) from e
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}
    except Exception as e:
        logger.exception("Update MCP server error: %s", e)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


async def patch_mcp_server(
    name: str,
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """PATCH enable-only bodies toggle enabled; any other body is an update."""
    body = await _request_json_mapping(request)
    keys = {key for key in body if key not in {"scope", "project_id"}}
    if keys == {"enabled"}:
        return await set_mcp_server_enabled(name, request, server, body=body)
    return await update_mcp_server(name, request, server, body=body)


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

        current_project_id = request_mcp_scope(request, server, body)
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
            llm_service=server.llm_service,
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
        logger.exception("Import MCP server error: %s", e)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


async def remove_mcp_server(
    name: str,
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """Remove the exact (name, resolved-project) MCP server row."""
    start_time = time.perf_counter()

    try:
        if server.mcp_manager is None:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "MCP manager not available",
                "response_time_ms": response_time_ms,
            }

        scope_project = request_mcp_scope(request, server, None)
        row = _exact_server(server.mcp_manager, name, scope_project)
        server_id = getattr(row, "id", None)
        if not isinstance(server_id, str) or not server_id:
            raise _unknown_server(name)
        await server.mcp_manager.remove_server(server_id, project_id=scope_project)
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

    except HTTPException:
        raise
    except ValueError as e:
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}
    except Exception as e:
        logger.exception("Remove MCP server error: %s", e)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


async def set_mcp_server_enabled(
    name: str,
    request: Request,
    server: "HTTPServer" = Depends(get_server),
    body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Enable or disable the exact (name, resolved-project) instance."""
    start_time = time.perf_counter()

    try:
        if body is None:
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

        scope_project = request_mcp_scope(request, server, body)
        row = _exact_server(server.mcp_manager, name, scope_project)
        server_id = getattr(row, "id", None)
        if not isinstance(server_id, str) or not server_id:
            raise _unknown_server(name)
        result = await server.mcp_manager.set_server_enabled(
            server_id,
            enabled,
            project_id=scope_project,
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
        logger.exception(
            "Set MCP server enabled error",
            extra={"server_name": name, "enabled": enabled, "error": str(e)},
        )
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


__all__ = [
    "list_mcp_servers",
    "add_mcp_server",
    "update_mcp_server",
    "patch_mcp_server",
    "import_mcp_server",
    "remove_mcp_server",
    "set_mcp_server_enabled",
]
