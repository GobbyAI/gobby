"""Server-name resolution helpers for the tool proxy service."""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Callable, Mapping
from typing import Any, cast

from gobby.mcp_proxy.models import MCPServerConfig
from gobby.mcp_proxy.server_list import compact_mcp_server_list
from gobby.mcp_proxy.services._manager_compat import manager_is_connected
from gobby.storage.projects import GLOBAL_PROJECT_ID

logger = logging.getLogger("gobby.mcp.server")


class ProjectScopeUnresolvedError(ValueError):
    """Explicit project scope could not be resolved to a registered project."""

    error_code = "project_scope_unresolved"

    def __init__(self, message: str = "project_scope_unresolved") -> None:
        super().__init__(message)


def manager_of(service: Any) -> Any:
    """Return the MCP client manager from a service or the manager itself."""
    data = getattr(service, "__dict__", None)
    if isinstance(data, dict) and "_mcp_manager" in data:
        nested = data["_mcp_manager"]
        if nested is not None:
            return nested
    return service


def iter_manager_configs(manager: Any) -> list[Any]:
    """Return configured servers from a manager-like object."""
    configs = getattr(manager, "server_configs", None)
    if callable(configs):
        try:
            maybe = configs()
        except TypeError:
            maybe = None
        if isinstance(maybe, list | tuple):
            configs = maybe
    if isinstance(configs, list | tuple):
        return list(configs)
    mapping = getattr(manager, "_configs", None)
    if isinstance(mapping, Mapping):
        return list(mapping.values())
    return []


def _config_id(config: Any) -> str | None:
    value = getattr(config, "id", None)
    return value if isinstance(value, str) and value else None


def _config_by_id(manager: Any, server_id: str) -> MCPServerConfig | None:
    getter = getattr(manager, "get_server_config", None)
    if callable(getter):
        config = getter(server_id)
        if isinstance(config, MCPServerConfig):
            return config
        if config is not None and _config_id(config) == server_id:
            return cast(MCPServerConfig, config)
    for config in iter_manager_configs(manager):
        if _config_id(config) == server_id:
            return cast(MCPServerConfig, config)
    return None


def find_config_ids(manager: Any, name: str, *, project_id: str) -> list[str]:
    """Return config ids for ``name``: exact project match first, then global."""
    wanted = name.lower()
    exact: list[str] = []
    global_ids: list[str] = []
    for config in iter_manager_configs(manager):
        config_name = getattr(config, "name", None)
        if not isinstance(config_name, str) or config_name.lower() != wanted:
            continue
        config_id = _config_id(config)
        if config_id is None:
            continue
        config_project = str(getattr(config, "project_id", "") or "")
        if config_project == project_id:
            exact.append(config_id)
        elif config_project == GLOBAL_PROJECT_ID:
            global_ids.append(config_id)
    return [*exact, *global_ids]


def resolve_server(
    service: Any,
    server_name: str | None = None,
    *,
    server_id: str | None = None,
    project_id: str,
) -> MCPServerConfig | None:
    """Resolve a server name or id to a config visible in ``project_id``."""
    manager = manager_of(service)
    if server_id:
        config = _config_by_id(manager, server_id)
        if config is None:
            return None
        config_project = str(getattr(config, "project_id", "") or "")
        if config_project in {project_id, GLOBAL_PROJECT_ID}:
            return config
        return None
    if not server_name:
        return None
    aliased = (
        resolve_server_name(service, server_name)
        if hasattr(service, "_SERVER_SUGGESTIONS")
        else server_name
    )
    ids = find_config_ids(manager, aliased, project_id=project_id)
    if ids:
        return _config_by_id(manager, ids[0])
    if aliased != server_name:
        ids = find_config_ids(manager, server_name, project_id=project_id)
        if ids:
            return _config_by_id(manager, ids[0])
    return resolve_server(service, server_id=server_name, project_id=project_id)


def as_project_id(value: object, *, default: str = GLOBAL_PROJECT_ID) -> str:
    """Return a real project UUID string, ignoring mock/empty values."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _nonempty(value: str | None) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def resolve_request_scope(
    *,
    session_project_id: str | None,
    project_id: str | None,
    scope: str | None,
    fallback_project_id: str,
    project_exists: Callable[[str], bool] | None = None,
) -> str:
    """Total function of explicit call inputs to the caller's project id."""
    scope_value = _nonempty(scope)
    if scope_value == "global":
        return GLOBAL_PROJECT_ID
    session = _nonempty(session_project_id)
    if session is not None:
        return session
    explicit = _nonempty(project_id)
    if explicit is not None:
        if project_exists is not None and not project_exists(explicit):
            raise ProjectScopeUnresolvedError()
        return explicit
    if scope_value == "project":
        raise ProjectScopeUnresolvedError()
    return fallback_project_id


def fallback_project_id(service: Any) -> str:
    """MCP front-door fallback: ambient project context, else manager, else global."""
    from gobby.utils.project_context import get_project_context

    ctx = get_project_context()
    if ctx and ctx.get("id"):
        return str(ctx["id"])
    return as_project_id(getattr(manager_of(service), "project_id", None))


def caller_project_id(
    service: Any,
    *,
    session_project_id: str | None = None,
    project_id: str | None = None,
    scope: str | None = None,
    project_exists: Callable[[str], bool] | None = None,
) -> str:
    """Resolve the caller's project for a proxy front-door call."""
    return resolve_request_scope(
        session_project_id=session_project_id,
        project_id=project_id,
        scope=scope,
        fallback_project_id=fallback_project_id(service),
        project_exists=project_exists,
    )


def resolved_server_id(manager: Any, name: str, *, project_id: str) -> str | None:
    """Resolve ``name`` in ``project_id`` to a manager config id.

    ``project_id`` is the caller's resolved scope. A missing scope is a caller
    bug and raises instead of silently resolving another project's row.
    """
    if not (isinstance(project_id, str) and project_id.strip()):
        raise ProjectScopeUnresolvedError()
    config = resolve_server(manager, name, project_id=project_id.strip())
    return None if config is None else config.id


def resolve_server_name(service: Any, server_name: str) -> str:
    """Auto-redirect known server name aliases to the correct server."""
    suggestions = getattr(service, "_SERVER_SUGGESTIONS", None)
    if isinstance(suggestions, Mapping):
        return cast("str", suggestions.get(server_name, server_name))
    return server_name


def get_server_suggestion(service: Any, server_name: str) -> str | None:
    """Get a suggestion for a possibly misspelled server name."""
    suggestions = getattr(service, "_SERVER_SUGGESTIONS", None)
    if isinstance(suggestions, Mapping):
        return cast("str | None", suggestions.get(server_name))
    return None


def is_proxy_namespace(service: Any, server_name: str) -> bool:
    """Check if the server name is the proxy namespace rather than a real server."""
    return bool(server_name == getattr(service, "_PROXY_NAMESPACE", None))


def resolve_server_for_tool(service: Any, tool_name: str) -> str | None:
    """Resolve the actual server name for a tool when given the proxy namespace."""
    resolved = cast("str | None", service.find_tool_server(tool_name))
    if resolved:
        logger.debug("Auto-resolved server_name='gobby' → '%s' for tool '%s'", resolved, tool_name)
    else:
        logger.debug("server_name='gobby' used but tool '%s' not found on any server", tool_name)
    return resolved


def find_tool_server(service: Any, tool_name: str, *, project_id: str | None = None) -> str | None:
    """Find which visible server owns a tool."""
    if service._internal_manager:
        server = cast("str | None", service._internal_manager.find_tool_server(tool_name))
        if server:
            return server

    scope = project_id or fallback_project_id(service)
    manager = manager_of(service)
    visible_ids = set()
    for config in iter_manager_configs(manager):
        config_id = _config_id(config)
        if config_id is None:
            continue
        config_project = str(getattr(config, "project_id", "") or "")
        if config_project == scope or config_project == GLOBAL_PROJECT_ID:
            visible_ids.add(config_id)

    seen_names: set[str] = set()
    for config in iter_manager_configs(manager):
        config_id = _config_id(config)
        if config_id not in visible_ids:
            continue
        name = getattr(config, "name", None)
        if not isinstance(name, str) or name in seen_names:
            continue
        if str(getattr(config, "project_id", "") or "") == scope:
            seen_names.add(name)
        tools = getattr(config, "tools", None) or []
        for tool in tools:
            tool_name_in_config = (
                tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
            )
            if tool_name_in_config == tool_name:
                return name
    return None


def _scope_label(project_id: str | None) -> str:
    if project_id == GLOBAL_PROJECT_ID:
        return "global"
    return "project"


async def list_servers(
    service: Any,
    name_filter: str | None = None,
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    """List MCP servers visible to the caller's project, including internal registries."""
    server_list: list[dict[str, Any]] = []
    connected = 0
    if service._internal_manager:
        for reg in service._internal_manager.get_all_registries():
            server_list.append({"name": reg.name, "state": "connected", "transport": "internal"})
            connected += 1
    scope = project_id or fallback_project_id(service)
    manager = manager_of(service)
    project_names = {
        str(getattr(config, "name", ""))
        for config in iter_manager_configs(manager)
        if str(getattr(config, "project_id", "") or "") == scope
    }
    for config in iter_manager_configs(manager):
        config_project = str(getattr(config, "project_id", "") or "")
        name = getattr(config, "name", None)
        if not isinstance(name, str):
            continue
        if config_project == scope:
            pass
        elif config_project == GLOBAL_PROJECT_ID and name not in project_names:
            pass
        else:
            continue
        config_id = _config_id(config)
        health = manager.health.get(config_id) if config_id else None
        if health is None:
            health = manager.health.get(name)
        state = health.state.value if health is not None else "unknown"
        is_conn = await manager_is_connected(manager, config_id or name)
        if is_conn:
            connected += 1
        entry: dict[str, Any] = {
            "name": name,
            "state": state,
            "transport": getattr(config, "transport", None),
            "scope": _scope_label(config_project),
            "project_id": config_project,
            "id": config_id,
        }
        template = getattr(config, "template", None)
        if isinstance(template, str) and template:
            entry["template"] = template
        if not getattr(config, "enabled", True):
            entry["enabled"] = False
        server_list.append(entry)

    if name_filter:
        server_list = [s for s in server_list if fnmatch.fnmatch(s["name"], name_filter)]
        connected = sum(1 for s in server_list if s.get("state") == "connected")

    return compact_mcp_server_list(
        {
            "success": True,
            "servers": server_list,
            "total": len(server_list),
            "connected": connected,
        }
    )
