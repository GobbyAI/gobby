"""Connection lifecycle helpers for the MCP client manager facade."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, cast

from mcp import ClientSession

from gobby.mcp_proxy.connection_cleanup import (
    clear_connection_state,
    disconnect_connection,
    finalize_disconnect_all,
)
from gobby.mcp_proxy.lazy import CircuitBreakerOpen
from gobby.mcp_proxy.models import ConnectionState, MCPConnectionHealth, MCPError, MCPServerConfig
from gobby.mcp_proxy.transports.base import BaseTransportConnection

CreateConnection = Callable[[MCPServerConfig], BaseTransportConnection]


def _dedupe_configs_by_id(configs: list[MCPServerConfig]) -> list[MCPServerConfig]:
    deduped: dict[str, MCPServerConfig] = {}
    for config in configs:
        deduped.setdefault(config.id, config)
    return list(deduped.values())


async def _acquire_connection_lock(manager: Any, server_id: str) -> asyncio.Lock:
    lock = cast(asyncio.Lock, manager._lazy_connector.get_connection_lock(server_id))
    try:
        await asyncio.wait_for(lock.acquire(), timeout=manager.connection_timeout)
    except TimeoutError as exc:
        config = manager._configs.get(server_id)
        label = config.name if config is not None else server_id
        error = MCPError(
            f"Timed out waiting for connection lock for '{label}' "
            f"after {manager.connection_timeout}s"
        )
        manager._lazy_connector.mark_failed(server_id, str(error))
        raise error from exc
    return lock


def _require_connection_attempt(manager: Any, server_id: str) -> None:
    if manager._lazy_connector.can_attempt_connection(server_id):
        return
    state = manager._lazy_connector.get_state(server_id)
    config = manager._configs.get(server_id)
    label = config.name if config is not None else server_id
    if state and state.circuit_breaker.last_failure_time:
        elapsed = time.time() - state.circuit_breaker.last_failure_time
        recovery_in = max(0, state.circuit_breaker.recovery_timeout - elapsed)
        raise CircuitBreakerOpen(label, recovery_in)
    raise MCPError(f"Circuit breaker open for '{label}'")


async def _connect_with_retries(
    manager: Any,
    server_id: str,
    config: MCPServerConfig,
) -> ClientSession:
    retry_config = manager._lazy_connector.retry_config
    last_error: Exception | None = None
    label = config.name

    for attempt in range(retry_config.max_retries + 1):
        try:
            state = manager._lazy_connector.get_state(server_id)
            if state:
                state.record_connection_attempt()

            session = await asyncio.wait_for(
                manager._connect_server(config),
                timeout=manager.connection_timeout,
            )

            if session:
                manager._lazy_connector.mark_connected(server_id)
                return cast(ClientSession, session)
            raise MCPError(f"Connection returned no session for '{label}'")
        except MCPError as exc:
            if exc.missing_secrets:
                raise
            last_error = exc
            manager._lazy_connector.mark_failed(server_id, str(exc))
        except TimeoutError:
            last_error = MCPError(f"Connection timeout after {manager.connection_timeout}s")
            manager._lazy_connector.mark_failed(server_id, str(last_error))
        except Exception as exc:
            last_error = exc
            manager._lazy_connector.mark_failed(server_id, str(exc))

        if attempt < retry_config.max_retries:
            delay = retry_config.get_delay(attempt)
            logging.getLogger("gobby.mcp.manager").warning(
                "Connection to '%s' failed (attempt %s/%s), retrying in %.1fs: %s",
                label,
                attempt + 1,
                retry_config.max_retries + 1,
                delay,
                last_error,
            )
            await asyncio.sleep(delay)

    raise MCPError(
        f"Failed to connect to '{label}' after {retry_config.max_retries + 1} attempts: {last_error}"
    ) from last_error


async def connect_all(manager: Any, configs: list[MCPServerConfig] | None) -> dict[str, bool]:
    """Connect configured MCP servers according to lazy/eager settings."""
    manager._running = True
    results: dict[str, bool] = {}

    configs_to_connect = configs if configs is not None else manager.server_configs

    if configs:
        configs_to_connect = configs
        for config in configs:
            manager._configs[config.id] = config
            if config.enabled:
                manager._lazy_connector.register_server(config.id)

    for config in manager.server_configs:
        if config.id not in manager.health:
            initial_state = (
                ConnectionState.PENDING
                if manager.lazy_connect and config.name not in manager.preconnect_servers
                else ConnectionState.DISCONNECTED
            )
            manager.health[config.id] = MCPConnectionHealth(
                name=config.name,
                state=initial_state,
                project_id=config.project_id,
            )

    if manager._health_check_task is None:
        manager._health_check_task = asyncio.create_task(manager._monitor_health())

    if manager.lazy_connect:
        configs_to_connect = [
            config for config in configs_to_connect if config.name in manager.preconnect_servers
        ]
        if configs_to_connect:
            logging.getLogger("gobby.mcp.manager").info(
                "Lazy mode: preconnecting %s servers (%s)",
                len(configs_to_connect),
                ", ".join(config.name for config in configs_to_connect),
            )
        else:
            logging.getLogger("gobby.mcp.manager").info(
                "Lazy mode: no preconnect servers configured, %s servers available on-demand",
                len(manager._configs),
            )

    configs_to_connect = _dedupe_configs_by_id(configs_to_connect)
    connect_tasks: list[asyncio.Task[ClientSession | None]] = []
    bound_configs: list[MCPServerConfig] = []
    for config in configs_to_connect:
        if not config.enabled:
            logging.getLogger("gobby.mcp.manager").debug(
                "Skipping disabled server: %s",
                config.name,
            )
            results[config.id] = False
            continue

        connect_tasks.append(asyncio.create_task(manager._connect_server(config)))
        bound_configs.append(config)

    if not connect_tasks:
        return results

    task_results = await asyncio.gather(*connect_tasks, return_exceptions=True)

    for config, result in zip(bound_configs, task_results, strict=True):
        if isinstance(result, Exception):
            logging.getLogger("gobby.mcp.manager").error(
                "Failed to connect to %s: %s",
                config.name,
                result,
            )
            results[config.id] = False
        else:
            results[config.id] = bool(result)
            if result:
                manager._lazy_connector.mark_connected(config.id)

    return results


async def connect_server(
    manager: Any,
    config: MCPServerConfig,
    create_connection: CreateConnection,
) -> ClientSession | None:
    """Connect to a single server and update manager health/runtime state."""
    if config.id not in manager.health:
        manager.health[config.id] = MCPConnectionHealth(
            name=config.name,
            state=ConnectionState.DISCONNECTED,
            project_id=config.project_id,
        )

    try:
        resolved_config = await asyncio.to_thread(manager._resolve_secrets_in_config, config)

        connection = manager._connections.get(config.id)
        if connection is None:
            connection = create_connection(resolved_config)
            manager._connections[config.id] = connection
        else:
            connection.config = resolved_config

        manager.health[config.id].state = ConnectionState.CONNECTING
        manager.health[config.id].name = config.name
        manager.health[config.id].project_id = config.project_id

        session = await connection.connect()

        manager.health[config.id].state = ConnectionState.CONNECTED
        manager.health[config.id].missing_secrets = None
        manager.health[config.id].record_success()

        return cast(ClientSession | None, session)
    except MCPError as exc:
        if exc.missing_secrets:
            from gobby.mcp_proxy.client_manager.server_registry import _set_health

            _set_health(
                manager,
                config,
                ConnectionState.NEEDS_CONFIGURATION,
                missing_secrets=list(exc.missing_secrets),
                last_error=str(exc),
            )
            manager._connections.pop(config.id, None)
        else:
            manager.health[config.id].state = ConnectionState.FAILED
            manager.health[config.id].record_failure(str(exc))
        raise
    except Exception as exc:
        manager.health[config.id].state = ConnectionState.FAILED
        manager.health[config.id].record_failure(str(exc))
        raise


async def disconnect_all(manager: Any, logger: logging.Logger) -> None:
    """Disconnect all active connections and background health tasks."""
    manager._running = False
    try:
        await finalize_disconnect_all(
            connections=manager._connections,
            health=manager.health,
            lazy_connector=manager._lazy_connector,
            reconnect_tasks=manager._reconnect_tasks,
            health_check_task=manager._health_check_task,
            logger=logger,
            tool_schema_cache=manager._tool_schema_cache,
        )
    finally:
        manager._health_check_task = None


async def disconnect_server(manager: Any, server_id: str, logger: logging.Logger) -> None:
    """Best-effort disconnect of one server without removing its config."""
    connection = clear_connection_state(
        server_id,
        manager._connections,
        manager.health,
        manager._lazy_connector,
        tool_schema_cache=manager._tool_schema_cache,
    )
    if connection is not None:
        await disconnect_connection(server_id, connection, logger)
    manager.health.pop(server_id, None)


async def ensure_connected(manager: Any, server_id: str) -> ClientSession:
    """Ensure a server has an active session, connecting lazily if needed."""
    if server_id not in manager._configs:
        raise KeyError(f"Server '{server_id}' not configured")

    config = manager._configs[server_id]

    if not config.enabled:
        raise MCPError(f"Server '{config.name}' is disabled")

    if server_id in manager._connections:
        connection = manager._connections[server_id]
        if connection.is_connected and connection.session:
            return cast(ClientSession, connection.session)

    _require_connection_attempt(manager, server_id)

    lock = await _acquire_connection_lock(manager, server_id)
    try:
        if server_id in manager._connections:
            connection = manager._connections[server_id]
            if connection.is_connected and connection.session:
                return cast(ClientSession, connection.session)

        config = manager._configs[server_id]
        if config.template_id:
            from gobby.mcp_proxy.client_manager.server_registry import reexpand_template_config

            expanded = reexpand_template_config(manager, config, raise_on_stale=True)
            if expanded is not None:
                manager._configs[server_id] = expanded
                config = expanded

        return await _connect_with_retries(manager, server_id, config)
    finally:
        lock.release()


async def get_client_session(manager: Any, server_id: str) -> ClientSession:
    """Get an active MCP client session for a server."""
    return cast(ClientSession, await manager.ensure_connected(server_id))


async def reconnect(manager: Any, server_id: str, logger: logging.Logger) -> None:
    """Attempt to reconnect one server by refreshing it."""
    if server_id not in manager._configs:
        return

    try:
        await manager.refresh_server(server_id)
    except Exception as exc:
        config = manager._configs.get(server_id)
        label = config.name if config is not None else server_id
        logger.error("Reconnection failed for %s: %s", label, exc)
