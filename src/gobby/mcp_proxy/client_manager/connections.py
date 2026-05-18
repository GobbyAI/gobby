"""Connection lifecycle helpers for the MCP client manager facade."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any, cast

from mcp import ClientSession

from gobby.mcp_proxy.bundled import normalize_bundled_server_config
from gobby.mcp_proxy.connection_cleanup import (
    clear_connection_state,
    disconnect_connection,
    finalize_disconnect_all,
)
from gobby.mcp_proxy.lazy import CircuitBreakerOpen
from gobby.mcp_proxy.models import ConnectionState, MCPConnectionHealth, MCPError, MCPServerConfig
from gobby.mcp_proxy.transports.base import BaseTransportConnection

CreateConnection = Callable[
    [MCPServerConfig, str | None, Callable[[], Coroutine[Any, Any, str]] | None],
    BaseTransportConnection,
]


async def connect_all(manager: Any, configs: list[MCPServerConfig] | None) -> dict[str, bool]:
    """Connect configured MCP servers according to lazy/eager settings."""
    manager._running = True
    results: dict[str, bool] = {}

    configs_to_connect = configs if configs is not None else manager.server_configs

    if configs:
        normalized_configs = [normalize_bundled_server_config(config) for config in configs]
        configs_to_connect = normalized_configs
        for config in normalized_configs:
            manager._configs[config.name] = config
            manager._lazy_connector.register_server(config.name)

    for config in manager.server_configs:
        if config.name not in manager.health:
            initial_state = (
                ConnectionState.PENDING
                if manager.lazy_connect and config.name not in manager.preconnect_servers
                else ConnectionState.DISCONNECTED
            )
            manager.health[config.name] = MCPConnectionHealth(
                name=config.name,
                state=initial_state,
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

    connect_tasks: list[asyncio.Task[ClientSession | None]] = []
    bound_configs: list[MCPServerConfig] = []
    for config in configs_to_connect:
        if not config.enabled:
            logging.getLogger("gobby.mcp.manager").debug(
                "Skipping disabled server: %s",
                config.name,
            )
            results[config.name] = False
            continue

        connect_tasks.append(asyncio.create_task(manager._connect_server(config)))
        bound_configs.append(config)

    if not connect_tasks:
        return results

    task_results = await asyncio.gather(*connect_tasks, return_exceptions=True)

    for config, result in zip(bound_configs, task_results, strict=False):
        if isinstance(result, Exception):
            logging.getLogger("gobby.mcp.manager").error(
                "Failed to connect to %s: %s",
                config.name,
                result,
            )
            results[config.name] = False
        else:
            results[config.name] = bool(result)
            if result:
                manager._lazy_connector.mark_connected(config.name)

    return results


async def connect_server(
    manager: Any,
    config: MCPServerConfig,
    create_connection: CreateConnection,
) -> ClientSession | None:
    """Connect to a single server and update manager health/runtime state."""
    if config.name not in manager.health:
        manager.health[config.name] = MCPConnectionHealth(
            name=config.name,
            state=ConnectionState.DISCONNECTED,
        )

    try:
        resolved_config = manager._resolve_secrets_in_config(config)

        if config.name not in manager._connections:
            connection = create_connection(
                resolved_config,
                manager._auth_token,
                manager._token_refresh_callback,
            )
            manager._connections[config.name] = connection

        connection = manager._connections[config.name]
        manager.health[config.name].state = ConnectionState.CONNECTING

        session = await connection.connect()

        manager.health[config.name].state = ConnectionState.CONNECTED
        manager.health[config.name].record_success()

        return cast(ClientSession | None, session)
    except Exception as exc:
        manager.health[config.name].state = ConnectionState.FAILED
        manager.health[config.name].record_failure(str(exc))
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
        )
    finally:
        manager._health_check_task = None


async def disconnect_server(manager: Any, name: str, logger: logging.Logger) -> None:
    """Best-effort disconnect of one server without removing its config."""
    connection = clear_connection_state(
        name,
        manager._connections,
        manager.health,
        manager._lazy_connector,
    )
    if connection is not None:
        await disconnect_connection(name, connection, logger)
    manager.health.pop(name, None)


async def ensure_connected(manager: Any, server_name: str) -> ClientSession:
    """Ensure a server has an active session, connecting lazily if needed."""
    if server_name not in manager._configs:
        raise KeyError(f"Server '{server_name}' not configured")

    config = manager._configs[server_name]

    if not config.enabled:
        raise MCPError(f"Server '{server_name}' is disabled")

    if server_name in manager._connections:
        connection = manager._connections[server_name]
        if connection.is_connected and connection.session:
            return cast(ClientSession, connection.session)

    if not manager._lazy_connector.can_attempt_connection(server_name):
        state = manager._lazy_connector.get_state(server_name)
        if state and state.circuit_breaker.last_failure_time:
            elapsed = time.time() - state.circuit_breaker.last_failure_time
            recovery_in = max(0, state.circuit_breaker.recovery_timeout - elapsed)
            raise CircuitBreakerOpen(server_name, recovery_in)
        raise MCPError(f"Circuit breaker open for '{server_name}'")

    async with manager._lazy_connector.get_connection_lock(server_name):
        if server_name in manager._connections:
            connection = manager._connections[server_name]
            if connection.is_connected and connection.session:
                return cast(ClientSession, connection.session)

        retry_config = manager._lazy_connector.retry_config
        last_error: Exception | None = None

        for attempt in range(retry_config.max_retries + 1):
            try:
                state = manager._lazy_connector.get_state(server_name)
                if state:
                    state.record_connection_attempt()

                session = await asyncio.wait_for(
                    manager._connect_server(config),
                    timeout=manager.connection_timeout,
                )

                if session:
                    manager._lazy_connector.mark_connected(server_name)
                    return cast(ClientSession, session)
                raise MCPError(f"Connection returned no session for '{server_name}'")
            except TimeoutError:
                last_error = MCPError(f"Connection timeout after {manager.connection_timeout}s")
                manager._lazy_connector.mark_failed(server_name, str(last_error))
            except Exception as exc:
                last_error = exc
                manager._lazy_connector.mark_failed(server_name, str(exc))

            if attempt < retry_config.max_retries:
                delay = retry_config.get_delay(attempt)
                logging.getLogger("gobby.mcp.manager").warning(
                    "Connection to '%s' failed (attempt %s/%s), retrying in %.1fs: %s",
                    server_name,
                    attempt + 1,
                    retry_config.max_retries + 1,
                    delay,
                    last_error,
                )
                await asyncio.sleep(delay)

        raise MCPError(
            f"Failed to connect to '{server_name}' after "
            f"{retry_config.max_retries + 1} attempts: {last_error}"
        ) from last_error


async def get_client_session(manager: Any, server_name: str) -> ClientSession:
    """Get an active MCP client session for a server."""
    return cast(ClientSession, await manager.ensure_connected(server_name))


async def reconnect(manager: Any, server_name: str, logger: logging.Logger) -> None:
    """Attempt to reconnect one server."""
    if server_name not in manager._configs:
        return

    config = manager._configs[server_name]

    old_connection = manager._connections.pop(server_name, None)
    if old_connection is not None:
        try:
            await asyncio.wait_for(old_connection.disconnect(), timeout=5.0)
        except TimeoutError:
            logger.warning("Old connection disconnect timed out for %s", server_name)
        except Exception as exc:
            logger.warning("Error disconnecting old %s connection: %s", server_name, exc)

    try:
        logger.info("Reconnecting %s...", server_name)
        await manager._connect_server(config)
        logger.info("Successfully reconnected %s", server_name)
    except Exception as exc:
        logger.error("Reconnection failed for %s: %s", server_name, exc)
