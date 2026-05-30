"""Server registry helpers for the MCP client manager facade."""

from __future__ import annotations

import logging
from typing import Any, Protocol, cast

from gobby.mcp_proxy.bundled import normalize_bundled_server_config
from gobby.mcp_proxy.models import ConnectionState, MCPConnectionHealth, MCPServerConfig
from gobby.mcp_proxy.transports.base import BaseTransportConnection

LOGGER = logging.getLogger("gobby.mcp.manager")


class _CachedToolsManager(Protocol):
    def get_cached_tools(self, server_name: str, *, project_id: str) -> list[Any]: ...


def truncate_tool_brief(text: str | None, *, max_chars: int = 100) -> str:
    if not text:
        return ""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return f"{text[: max_chars - 1]}…"


_truncate_tool_brief = truncate_tool_brief


def load_tools_from_db(
    mcp_db_manager: _CachedToolsManager,
    server_name: str,
    project_id: str,
    logger: logging.Logger,
) -> list[dict[str, str]] | None:
    """Load cached lightweight tool metadata for an MCP server."""
    try:
        tools = mcp_db_manager.get_cached_tools(server_name, project_id=project_id)
        if not tools:
            return None
        return [
            {
                "name": tool.name,
                "brief": truncate_tool_brief(tool.description),
            }
            for tool in tools
        ]
    except Exception as exc:
        logger.warning("Failed to load cached tools for '%s': %s", server_name, exc)
        return None


def load_initial_configs(
    manager: Any,
    server_configs: list[MCPServerConfig] | None,
    logger: logging.Logger,
) -> None:
    """Populate manager config state from explicit configs or the MCP DB."""
    if server_configs is None and manager.mcp_db_manager is not None:
        if manager.project_id:
            db_servers = manager.mcp_db_manager.list_runtime_servers(
                project_id=manager.project_id,
                enabled_only=False,
            )
        else:
            db_servers = manager.mcp_db_manager.list_all_servers(enabled_only=False)

        for server in db_servers:
            config = MCPServerConfig(
                name=server.name,
                transport=server.transport,
                url=server.url,
                command=server.command,
                args=server.args,
                env=server.env,
                headers=server.headers,
                enabled=server.enabled,
                description=server.description,
                project_id=server.project_id,
                tools=manager.load_tools_from_db(
                    manager.mcp_db_manager,
                    server.name,
                    server.project_id,
                ),
            )
            manager._configs[config.name] = config
            manager._lazy_connector.register_server(config.name)
        logger.info("Loaded %s MCP servers from database", len(manager._configs))
    elif server_configs:
        for config in server_configs:
            manager._configs[config.name] = config
            manager._lazy_connector.register_server(config.name)


def get_server_config(manager: Any, name: str) -> MCPServerConfig | None:
    """Return a configured MCP server by name."""
    return cast(MCPServerConfig | None, manager._configs.get(name))


def list_connections(manager: Any) -> list[MCPServerConfig]:
    """List configs for currently tracked connections."""
    return [manager._configs[name] for name in manager._connections.keys()]


def get_available_servers(manager: Any) -> list[str]:
    """Return all configured MCP server names."""
    return list(manager._configs.keys())


def get_client(manager: Any, server_name: str) -> BaseTransportConnection:
    """Return an active transport connection for a configured server."""
    if server_name not in manager._configs:
        raise ValueError(f"Unknown MCP server: '{server_name}'")
    if server_name in manager._connections:
        return cast(BaseTransportConnection, manager._connections[server_name])
    raise ValueError(f"Client '{server_name}' not connected")


def has_server(manager: Any, server_name: str) -> bool:
    """Return whether a server is configured."""
    return server_name in manager._configs


def is_connected(manager: Any, server_name: str) -> bool:
    """Return whether a server has a tracked runtime connection."""
    return server_name in manager._connections


async def _discover_and_cache_tools(
    manager: Any,
    config: MCPServerConfig,
    session: Any | None,
) -> list[dict[str, Any]]:
    """List tools from a connected session and cache their full schemas."""
    if session is None:
        return []

    try:
        tools_result = await session.list_tools()
        tool_schemas = [
            {
                "name": tool.name,
                "description": getattr(tool, "description", "") or "",
                "inputSchema": getattr(tool, "inputSchema", {}) or {},
            }
            for tool in tools_result.tools
        ]
    except Exception as exc:
        LOGGER.warning("Failed to list tools for %s: %s", config.name, exc)
        return []

    if tool_schemas:
        manager.cache_discovered_tools(config.name, tool_schemas)
    return tool_schemas


async def add_server(manager: Any, config: MCPServerConfig) -> dict[str, Any]:
    """Add a server config, persist it, and discover tools if enabled."""
    config = normalize_bundled_server_config(config)
    if config.name in manager._configs:
        raise ValueError(f"MCP server '{config.name}' already exists")

    manager._configs[config.name] = config
    manager._lazy_connector.register_server(config.name)

    if manager.mcp_db_manager and config.project_id:
        manager.mcp_db_manager.upsert(
            name=config.name,
            transport=config.transport,
            project_id=config.project_id,
            url=config.url,
            command=config.command,
            args=config.args,
            env=config.env,
            headers=config.headers,
            enabled=config.enabled,
            description=config.description,
        )

    tool_schemas: list[dict[str, Any]] = []
    if config.enabled:
        session = await manager._connect_server(config)
        tool_schemas = await _discover_and_cache_tools(manager, config, session)

    return {
        "success": True,
        "name": config.name,
        "full_tool_schemas": tool_schemas,
    }


async def remove_server(
    manager: Any,
    name: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Remove server config and runtime state."""
    if name not in manager._configs:
        raise ValueError(f"MCP server '{name}' not found")

    config = manager._configs[name]
    if config.transport == "internal":
        raise ValueError(f"Internal MCP server '{name}' cannot be removed")
    effective_project_id = project_id or config.project_id

    if name in manager._connections:
        await manager._connections[name].disconnect()
        del manager._connections[name]

    del manager._configs[name]
    manager.health.pop(name, None)
    manager._lazy_connector.unregister_server(name)

    if manager.mcp_db_manager and effective_project_id:
        manager.mcp_db_manager.remove_server(name, effective_project_id)

    return {"success": True, "name": name}


async def set_server_enabled(
    manager: Any,
    name: str,
    enabled: bool,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Enable or disable an external server, persist it, and (dis)connect.

    Enabling connects the server and discovers its tools (mirroring
    ``add_server``); disabling tears down any live connection. Internal
    registries are not tracked here, so callers should only target external
    servers.
    """
    if name not in manager._configs:
        raise ValueError(f"MCP server '{name}' not found")

    config = manager._configs[name]
    if config.transport == "internal":
        raise ValueError(f"Internal MCP server '{name}' cannot be enabled or disabled")

    effective_project_id = project_id or config.project_id

    if config.enabled == enabled:
        return {"success": True, "name": name, "enabled": enabled}

    if enabled:
        session = await manager._connect_server(config)
        await _discover_and_cache_tools(manager, config, session)
        if manager.mcp_db_manager and effective_project_id:
            try:
                manager.mcp_db_manager.update_server(name, effective_project_id, enabled=True)
            except Exception:
                try:
                    if name in manager._connections:
                        await manager._connections[name].disconnect()
                        del manager._connections[name]
                except Exception:
                    LOGGER.warning(
                        "Failed to clean up MCP server connection after enable rollback",
                        exc_info=True,
                    )
                manager.health.pop(name, None)
                raise
        config.enabled = True
        manager._lazy_connector.register_server(name)
    else:
        if manager.mcp_db_manager and effective_project_id:
            manager.mcp_db_manager.update_server(name, effective_project_id, enabled=False)
        config.enabled = False
        if name in manager._connections:
            await manager._connections[name].disconnect()
            del manager._connections[name]
        manager.health.pop(name, None)
        manager._lazy_connector.unregister_server(name)

    return {"success": True, "name": name, "enabled": enabled}


def server_configs(manager: Any) -> list[MCPServerConfig]:
    """Return all configured MCP servers."""
    return list(manager._configs.values())


def add_server_config(manager: Any, config: MCPServerConfig) -> None:
    """Register a server config for future lazy/eager connection."""
    config = normalize_bundled_server_config(config)
    manager._configs[config.name] = config
    manager._lazy_connector.register_server(config.name)
    if config.name not in manager.health:
        initial_state = (
            ConnectionState.PENDING if manager.lazy_connect else ConnectionState.DISCONNECTED
        )
        manager.health[config.name] = MCPConnectionHealth(name=config.name, state=initial_state)


def remove_server_config(manager: Any, name: str, logger: logging.Logger) -> None:
    """Remove a server config after callers have disconnected it."""
    if name in manager._connections:
        logger.warning(
            "Removing config for '%s' but connection still exists. "
            "You should disconnect the server first.",
            name,
        )
        raise RuntimeError(
            f"Cannot remove config for connected server '{name}'. Disconnect it first."
        )

    if name in manager._configs:
        del manager._configs[name]
