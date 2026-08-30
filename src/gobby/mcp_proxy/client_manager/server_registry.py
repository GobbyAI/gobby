"""Server registry helpers for the MCP client manager facade."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Protocol, cast

from gobby.mcp_proxy.models import (
    ConnectionState,
    MCPConnectionHealth,
    MCPError,
    MCPServerConfig,
    TemplateOwnedFieldsError,
    TemplateValuesInvalidError,
)
from gobby.mcp_proxy.transports.base import BaseTransportConnection
from gobby.storage.projects import GLOBAL_PROJECT_ID

LOGGER = logging.getLogger("gobby.mcp.manager")


class _CachedToolsManager(Protocol):
    def get_cached_tools(self, server_id: str) -> list[Any]: ...


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


def load_tools_from_db(
    mcp_db_manager: _CachedToolsManager,
    server_id: str,
    logger: logging.Logger,
) -> list[dict[str, str]] | None:
    """Load cached lightweight tool metadata for an MCP server."""
    try:
        tools = mcp_db_manager.get_cached_tools(server_id)
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
        logger.warning("Failed to load cached tools for '%s': %s", server_id, exc)
        return None


def config_from_server(server: Any, tools: list[dict[str, str]] | None = None) -> MCPServerConfig:
    """Build a manager config from a stored MCP server row."""
    data = dict(server.to_config())
    if tools is not None:
        data["tools"] = tools
    return MCPServerConfig(**data)


def visible_configs(manager: Any, project_id: str) -> list[MCPServerConfig]:
    """Return configs visible to ``project_id``, with project rows shadowing globals."""
    configs = [cast(MCPServerConfig, config) for config in manager._configs.values()]
    project_names = {config.name for config in configs if config.project_id == project_id}
    visible: list[MCPServerConfig] = []
    for config in configs:
        if config.project_id == project_id:
            visible.append(config)
        elif config.project_id == GLOBAL_PROJECT_ID and config.name not in project_names:
            visible.append(config)
    return visible


def _set_health(
    manager: Any,
    config: MCPServerConfig,
    state: ConnectionState,
    *,
    missing_secrets: list[str] | None = None,
    last_error: str | None = None,
) -> MCPConnectionHealth:
    health = manager.health.get(config.id)
    if health is None:
        health = MCPConnectionHealth(
            name=config.name,
            state=state,
            project_id=config.project_id,
        )
        manager.health[config.id] = health
    health.name = config.name
    health.project_id = config.project_id
    health.state = state
    health.missing_secrets = missing_secrets
    if last_error is not None:
        health.last_error = last_error
    return cast(MCPConnectionHealth, health)


def _scope_label(project_id: str) -> str:
    return "global" if project_id == GLOBAL_PROJECT_ID else project_id


def _unknown_server_error(server_id: str) -> MCPError:
    return MCPError(f"Unknown MCP server: '{server_id}'")


def _duplicate_server_error(config: MCPServerConfig) -> MCPError:
    return MCPError(f"MCP server '{config.name}' already exists")


def _stale_template_error(config: MCPServerConfig, message: str) -> MCPError:
    scope = _scope_label(config.project_id)
    return MCPError(
        f"Template instance '{config.name}' ({scope}) is stale: {message}. "
        f"Fix with `gobby mcp-proxy add-server --template {config.template or config.name} "
        f"--set …`"
    )


def probe_connectable(manager: Any, config: MCPServerConfig) -> bool:
    """Return True when secrets resolve; otherwise mark fail-closed health."""
    if not config.enabled:
        _set_health(manager, config, ConnectionState.DISABLED)
        return False
    try:
        manager._resolve_secrets_in_config(config)
    except MCPError as exc:
        if exc.missing_secrets:
            _set_health(
                manager,
                config,
                ConnectionState.NEEDS_CONFIGURATION,
                missing_secrets=list(exc.missing_secrets),
                last_error=str(exc),
            )
            return False
        raise
    return True


def reexpand_template_config(
    manager: Any,
    config: MCPServerConfig,
    *,
    raise_on_stale: bool,
) -> MCPServerConfig | None:
    """Re-expand a template-owned row. Return None when the instance is stale."""
    if not config.template_id or manager.mcp_db_manager is None:
        return config
    envelope = manager.mcp_db_manager.refresh_template_instances(
        manager._template_expand,
        server_id=config.id,
    )
    errors = envelope.get("errors") or {}
    error = errors.get(config.id) or errors.get(str(config.id))
    if error:
        message = error.get("error", str(error)) if isinstance(error, dict) else str(error)
        _set_health(
            manager,
            config,
            ConnectionState.STALE_TEMPLATE,
            last_error=message,
        )
        if raise_on_stale:
            raise _stale_template_error(config, message)
        return None
    row = manager.mcp_db_manager.get_server_by_id(config.id)
    if row is None:
        return config
    return config_from_server(row, tools=config.tools)


def load_initial_configs(
    manager: Any,
    server_configs: list[MCPServerConfig] | None,
    logger: logging.Logger,
) -> None:
    """Populate manager config state from explicit configs or the MCP DB."""
    loaded: list[MCPServerConfig] = []
    if server_configs is None and manager.mcp_db_manager is not None:
        db_servers = manager.mcp_db_manager.list_all_servers(enabled_only=False)
        for server in db_servers:
            tools = manager.load_tools_from_db(manager.mcp_db_manager, str(server.id))
            config = config_from_server(server, tools=tools)
            if config.template_id:
                expanded = reexpand_template_config(manager, config, raise_on_stale=False)
                if expanded is None:
                    manager._configs[config.id] = config
                    continue
                config = expanded
            loaded.append(config)
        logger.info("Loaded %s MCP servers from database", len(db_servers))
    elif server_configs:
        loaded = list(server_configs)

    for config in loaded:
        manager._configs[config.id] = config
        if not probe_connectable(manager, config):
            continue
        manager._lazy_connector.register_server(config.id)
        if config.id not in manager.health:
            initial_state = (
                ConnectionState.PENDING if manager.lazy_connect else ConnectionState.DISCONNECTED
            )
            _set_health(manager, config, initial_state)


def get_server_config(manager: Any, server_id: str) -> MCPServerConfig | None:
    """Return a configured MCP server by id."""
    return cast(MCPServerConfig | None, manager._configs.get(server_id))


def list_connections(manager: Any) -> list[MCPServerConfig]:
    """List configs for live transport connections."""
    return [
        manager._configs[server_id]
        for server_id, connection in manager._connections.items()
        if connection.is_connected and server_id in manager._configs
    ]


def get_available_servers(manager: Any, *, project_id: str) -> list[str]:
    """Return caller-visible MCP server names."""
    return [config.name for config in visible_configs(manager, project_id)]


def get_client(manager: Any, server_id: str) -> BaseTransportConnection:
    """Return an active transport connection for a configured server."""
    if server_id not in manager._configs:
        raise ValueError(f"Unknown MCP server: '{server_id}'")
    if server_id in manager._connections:
        return cast(BaseTransportConnection, manager._connections[server_id])
    raise ValueError(f"Client '{server_id}' not connected")


def has_server(manager: Any, server_id: str) -> bool:
    """Return whether a server is configured."""
    return server_id in manager._configs


def is_connected(manager: Any, server_id: str) -> bool:
    """Return whether a server has a live transport connection."""
    connection = manager._connections.get(server_id)
    return bool(connection is not None and connection.is_connected)


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
                "description": tool.description or "",
                "inputSchema": tool.input_schema or {},
            }
            for tool in tools_result.tools
        ]
    except Exception as exc:
        LOGGER.warning("Failed to list tools for %s: %s", config.name, exc)
        return []

    if tool_schemas:
        await asyncio.to_thread(manager.cache_discovered_tools, config.id, tool_schemas)
    return tool_schemas


def _existing_duplicate(manager: Any, config: MCPServerConfig) -> MCPServerConfig | None:
    for existing in manager._configs.values():
        if existing.name == config.name and existing.project_id == config.project_id:
            return cast(MCPServerConfig, existing)
    return None


async def add_server(manager: Any, config: MCPServerConfig) -> dict[str, Any]:
    """Add a server config, persist it, and discover tools if enabled."""
    config = replace(config)
    if manager.mcp_db_manager and config.project_id:
        row = await asyncio.to_thread(
            manager.mcp_db_manager.insert_server,
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
            requires_oauth=config.requires_oauth,
            oauth_provider=config.oauth_provider,
            connect_timeout=config.connect_timeout,
            template_id=config.template_id,
            template_values=config.template_values,
            runtime_hook=config.runtime_hook,
        )
        if row is None:
            raise _duplicate_server_error(config)
        config = config_from_server(row, tools=config.tools)
    elif _existing_duplicate(manager, config) is not None or config.id in manager._configs:
        raise _duplicate_server_error(config)

    manager._configs[config.id] = config
    manager._lazy_connector.register_server(config.id)

    tool_schemas: list[dict[str, Any]] = []
    connected = False
    connection_error: str | None = None
    if config.enabled:
        try:
            session = await manager._connect_server(config)
        except Exception as exc:
            connection_error = str(exc)
            LOGGER.warning("Failed to connect newly added MCP server %s: %s", config.name, exc)
        else:
            connected = session is not None
            tool_schemas = await _discover_and_cache_tools(manager, config, session)

    result: dict[str, Any] = {
        "success": True,
        "name": config.name,
        "id": config.id,
        "connected": connected,
        "full_tool_schemas": tool_schemas,
    }
    if connection_error is not None:
        result["error"] = connection_error
    return result


async def _locked(manager: Any, server_id: str) -> Any:
    from gobby.mcp_proxy.client_manager.connections import _acquire_connection_lock

    return await _acquire_connection_lock(manager, server_id)


async def remove_server(
    manager: Any,
    server_id: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Remove server config and runtime state."""
    if server_id not in manager._configs:
        raise ValueError(f"MCP server '{server_id}' not found")

    lock = await _locked(manager, server_id)
    try:
        if server_id not in manager._configs:
            raise ValueError(f"MCP server '{server_id}' not found")
        config = manager._configs[server_id]
        if config.transport == "internal":
            raise ValueError(f"Internal MCP server '{config.name}' cannot be removed")
        effective_project_id = project_id or config.project_id

        if manager.mcp_db_manager and effective_project_id:
            await asyncio.to_thread(
                manager.mcp_db_manager.remove_server, config.name, effective_project_id
            )

        connection = manager._connections.pop(server_id, None)
        try:
            if connection is not None:
                await connection.disconnect()
        finally:
            manager._tool_schema_cache.pop(server_id, None)
            del manager._configs[server_id]
            manager.health.pop(server_id, None)
            manager._lazy_connector.unregister_server(server_id)

        return {"success": True, "name": config.name, "id": server_id}
    finally:
        lock.release()


_TEMPLATE_OWNED_RUNTIME_FIELDS = (
    "transport",
    "url",
    "command",
    "args",
    "env",
    "headers",
    "connect_timeout",
)


async def update_server(
    manager: Any,
    server_id: str,
    config: MCPServerConfig | Mapping[str, Any],
    project_id: str | None = None,
) -> dict[str, Any]:
    """Update an external server under the per-id lock.

    A mapping ``config`` is a PATCH body: reread the row, merge, validate, persist.
    An ``MCPServerConfig`` replaces the live config as before.
    """
    if isinstance(config, Mapping):
        return await _update_server_patch(manager, server_id, config, project_id)
    return await _update_server_config(manager, server_id, config, project_id)


async def _update_server_config(
    manager: Any,
    server_id: str,
    config: MCPServerConfig,
    project_id: str | None = None,
) -> dict[str, Any]:
    config = replace(config)
    lock = await _locked(manager, server_id)
    try:
        existing = manager._configs.get(server_id)
        if existing is None:
            raise ValueError(f"MCP server '{server_id}' not found")
        if existing.transport == "internal":
            raise ValueError(f"Internal MCP server '{existing.name}' cannot be edited")
        if config.name != existing.name:
            raise ValueError("MCP server names cannot be changed")

        config.id = existing.id
        config.enabled = existing.enabled
        effective_project_id = project_id or config.project_id or existing.project_id
        if not config.project_id:
            config.project_id = effective_project_id

        config.validate()
        await _persist_and_replace(manager, existing, config, effective_project_id)
        return {"success": True, "name": config.name, "id": server_id}
    finally:
        lock.release()


async def _update_server_patch(
    manager: Any,
    server_id: str,
    patch: Mapping[str, Any],
    project_id: str | None = None,
) -> dict[str, Any]:
    lock = await _locked(manager, server_id)
    try:
        db = manager.mcp_db_manager
        row = db.get_server_by_id(server_id) if db is not None else None
        if row is None:
            manager._configs.pop(server_id, None)
            raise ValueError(f"MCP server '{server_id}' not found")
        existing = manager._configs.get(server_id) or config_from_server(row)
        if existing.transport == "internal":
            raise ValueError(f"Internal MCP server '{existing.name}' cannot be edited")
        config = config_from_server(row, tools=existing.tools)
        if existing.template_id:
            owned = [field for field in _TEMPLATE_OWNED_RUNTIME_FIELDS if field in patch]
            if owned:
                raise TemplateOwnedFieldsError(owned)
        if "values" in patch and config.template_id:
            _merge_template_values(manager, config, patch.get("values"))
        if "description" in patch:
            config.description = None if patch["description"] is None else str(patch["description"])
        config.validate()
        effective_project_id = project_id or config.project_id
        await _persist_and_replace(manager, existing, config, effective_project_id)
        return {
            "success": True,
            "name": config.name,
            "id": server_id,
            "template": config.template,
        }
    finally:
        lock.release()


def _merge_template_values(manager: Any, config: MCPServerConfig, values: Any) -> None:
    from gobby.mcp_proxy.templates import MCPServerTemplate, expand_template
    from gobby.storage.secrets import SecretStore

    if values is not None and not isinstance(values, Mapping):
        raise TemplateValuesInvalidError("values must be a JSON object")
    db = manager.mcp_db_manager
    template_row = db.get_template_by_id(config.template_id) if db is not None else None
    if template_row is None:
        raise TemplateValuesInvalidError("template not found")
    merged: dict[str, str] = {}
    for key, item in (config.template_values or {}).items():
        if isinstance(item, str):
            merged[str(key)] = item
    for key, item in dict(values or {}).items():
        if item is None:
            merged.pop(str(key), None)
        else:
            merged[str(key)] = item if isinstance(item, str) else str(item)
    definition = dict(template_row.definition)
    definition.setdefault("name", template_row.name)
    tmpl = MCPServerTemplate.from_definition(definition)
    secret_store = SecretStore(db.db) if getattr(db, "db", None) is not None else None

    def secret_exists(secret_name: str) -> bool:
        if secret_store is None:
            return False
        return bool(secret_store.exists(secret_name, project_id=config.project_id))

    try:
        expanded = expand_template(
            tmpl,
            name=config.name,
            project_id=config.project_id,
            values=merged,
            description=config.description,
            secret_exists=secret_exists,
        )
    except ValueError as exc:
        raise TemplateValuesInvalidError(str(exc)) from exc
    config.template_values = dict(expanded.config.template_values or expanded.template_values)
    config.transport = expanded.config.transport
    config.url = expanded.config.url
    config.command = expanded.config.command
    config.args = expanded.config.args
    config.env = expanded.config.env
    config.headers = expanded.config.headers
    config.connect_timeout = expanded.config.connect_timeout
    config.runtime_hook = expanded.config.runtime_hook
    config.template = expanded.config.template or config.template


async def _persist_and_replace(
    manager: Any,
    existing: MCPServerConfig,
    config: MCPServerConfig,
    effective_project_id: str | None,
) -> None:
    if manager.mcp_db_manager and effective_project_id:
        persisted = await asyncio.to_thread(
            manager.mcp_db_manager.update_server,
            existing.name,
            effective_project_id,
            transport=config.transport,
            url=config.url,
            command=config.command,
            args=config.args,
            env=config.env,
            headers=config.headers,
            enabled=config.enabled,
            description=config.description,
            requires_oauth=config.requires_oauth,
            oauth_provider=config.oauth_provider,
            connect_timeout=config.connect_timeout,
            template_id=config.template_id,
            template_values=config.template_values,
            runtime_hook=config.runtime_hook,
        )
        if persisted is None:
            raise ValueError(f"MCP server '{existing.name}' not found")
        config = config_from_server(persisted, tools=config.tools)

    connection = manager._connections.pop(config.id, None)
    try:
        if connection is not None:
            await connection.disconnect()
    finally:
        manager._tool_schema_cache.pop(config.id, None)
        manager.health.pop(config.id, None)
        manager._lazy_connector.unregister_server(config.id)
        manager._configs[config.id] = config
        if config.enabled:
            manager._lazy_connector.register_server(config.id)


async def set_server_description(manager: Any, server_id: str, description: str) -> None:
    """Persist a generated description and update the registered config."""
    config = manager._configs.get(server_id)
    if config is None:
        raise ValueError(f"MCP server '{server_id}' not found")

    if manager.mcp_db_manager and config.project_id:
        persisted = await asyncio.to_thread(
            manager.mcp_db_manager.update_server,
            config.name,
            config.project_id,
            description=description,
        )
        if persisted is None:
            raise RuntimeError(f"Persisted MCP server '{config.name}' not found")

    config.description = description


async def set_server_enabled(
    manager: Any,
    server_id: str,
    enabled: bool,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Enable or disable an external server, persist it, and (dis)connect."""
    if server_id not in manager._configs:
        raise ValueError(f"MCP server '{server_id}' not found")

    lock = await _locked(manager, server_id)
    try:
        if server_id not in manager._configs:
            raise ValueError(f"MCP server '{server_id}' not found")
        config = manager._configs[server_id]
        if config.transport == "internal":
            raise ValueError(f"Internal MCP server '{config.name}' cannot be enabled or disabled")

        effective_project_id = project_id or config.project_id

        if config.enabled == enabled:
            return {"success": True, "name": config.name, "id": server_id, "enabled": enabled}

        if enabled:
            manager._tool_schema_cache.pop(server_id, None)
            session = await manager._connect_server(config)
            await _discover_and_cache_tools(manager, config, session)
            if manager.mcp_db_manager and effective_project_id:
                try:
                    await asyncio.to_thread(
                        manager.mcp_db_manager.update_server,
                        config.name,
                        effective_project_id,
                        enabled=True,
                    )
                except Exception:
                    try:
                        if server_id in manager._connections:
                            await manager._connections[server_id].disconnect()
                            del manager._connections[server_id]
                    except Exception:
                        LOGGER.warning(
                            "Failed to clean up MCP server connection after enable rollback",
                            exc_info=True,
                        )
                    finally:
                        manager._tool_schema_cache.pop(server_id, None)
                    manager.health.pop(server_id, None)
                    raise
            config.enabled = True
            manager._lazy_connector.register_server(server_id)
        else:
            if manager.mcp_db_manager and effective_project_id:
                await asyncio.to_thread(
                    manager.mcp_db_manager.update_server,
                    config.name,
                    effective_project_id,
                    enabled=False,
                )
            config.enabled = False
            connection = manager._connections.pop(server_id, None)
            try:
                if connection is not None:
                    await connection.disconnect()
            finally:
                manager._tool_schema_cache.pop(server_id, None)
                manager.health.pop(server_id, None)
                manager._lazy_connector.unregister_server(server_id)

        return {"success": True, "name": config.name, "id": server_id, "enabled": enabled}
    finally:
        lock.release()


def server_configs(manager: Any) -> list[MCPServerConfig]:
    """Return all configured MCP servers."""
    return list(manager._configs.values())


def add_server_config(manager: Any, config: MCPServerConfig) -> None:
    """Register a server config for future lazy/eager connection."""
    manager._configs[config.id] = config
    if config.enabled:
        manager._lazy_connector.register_server(config.id)
    if config.id not in manager.health:
        if not config.enabled:
            initial_state = ConnectionState.DISABLED
        else:
            initial_state = (
                ConnectionState.PENDING if manager.lazy_connect else ConnectionState.DISCONNECTED
            )
        _set_health(manager, config, initial_state)


def remove_server_config(manager: Any, server_id: str, logger: logging.Logger) -> None:
    """Remove a server config after callers have disconnected it."""
    if server_id in manager._connections:
        logger.warning(
            "Removing config for '%s' but connection still exists. "
            "You should disconnect the server first.",
            server_id,
        )
        raise RuntimeError(
            f"Cannot remove config for connected server '{server_id}'. Disconnect it first."
        )

    if server_id in manager._configs:
        del manager._configs[server_id]
    manager._tool_schema_cache.pop(server_id, None)
    manager.health.pop(server_id, None)
    manager._lazy_connector.unregister_server(server_id)


async def refresh_server(manager: Any, server_id: str) -> None:
    """Rebuild one instance from its DB row and reconnect it."""
    from gobby.mcp_proxy.client_manager.connections import _connect_with_retries
    from gobby.mcp_proxy.connection_cleanup import disconnect_connection

    lock = await _locked(manager, server_id)
    try:
        db = manager.mcp_db_manager
        config = manager._configs.get(server_id)
        row = db.get_server_by_id(server_id) if db is not None else None
        if config is None and row is not None:
            config = config_from_server(row)

        if config is not None and config.template_id:
            expanded = reexpand_template_config(manager, config, raise_on_stale=True)
            if expanded is not None:
                config = expanded
            row = db.get_server_by_id(server_id) if db is not None else row

        if db is not None and row is None:
            connection = manager._connections.pop(server_id, None)
            manager._configs.pop(server_id, None)
            manager._tool_schema_cache.pop(server_id, None)
            manager.health.pop(server_id, None)
            manager._lazy_connector.unregister_server(server_id)
            if connection is not None:
                await disconnect_connection(server_id, connection, LOGGER)
            raise _unknown_server_error(server_id)

        if row is not None:
            config = config_from_server(row, tools=config.tools if config else None)
        elif config is None:
            raise _unknown_server_error(server_id)

        manager._configs[server_id] = config
        manager._tool_schema_cache.pop(server_id, None)
        manager._tool_cache_dirty.add(server_id)

        old_connection = manager._connections.pop(server_id, None)
        if old_connection is not None:
            teardown = asyncio.create_task(disconnect_connection(server_id, old_connection, LOGGER))
            try:
                await asyncio.shield(teardown)
            except asyncio.CancelledError:
                await teardown
                raise

        if not config.enabled:
            manager._tool_schema_cache.pop(server_id, None)
            manager._lazy_connector.unregister_server(server_id)
            _set_health(manager, config, ConnectionState.DISABLED)
            return

        # Secret resolution happens off-loop inside the connect path, which
        # owns missing-secret detection: connect_server reports
        # needs_configuration and never starts a transport.
        manager._lazy_connector.register_server(server_id)
        session = await _connect_with_retries(manager, server_id, config)
        await _discover_and_cache_tools(manager, config, session)
    finally:
        lock.release()
