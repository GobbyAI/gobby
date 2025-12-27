"""
Manager for multiple MCP client connections.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, cast

from mcp import ClientSession

from gobby.mcp_proxy.models import (
    ConnectionState,
    HealthState,
    MCPConnectionHealth,
    MCPError,
    MCPServerConfig,
)
from gobby.mcp_proxy.transports.base import BaseTransportConnection
from gobby.mcp_proxy.transports.factory import create_transport_connection

# Alias for backward compatibility with tests
_create_transport_connection = create_transport_connection

logger = logging.getLogger("gobby.mcp.manager")


class MCPClientManager:
    """
    Manages multiple MCP client connections with shared authentication.
    """

    def __init__(
        self,
        server_configs: list[MCPServerConfig] | None = None,
        token_refresh_callback: Callable[[], Coroutine[Any, Any, str]] | None = None,
        health_check_interval: float = 60.0,
        external_id: str | None = None,
        project_path: str | None = None,
        project_id: str | None = None,
    ):
        """
        Initialize manager.

        Args:
            server_configs: Initial list of server configurations
            token_refresh_callback: Async callback that returns fresh auth token
            health_check_interval: Seconds between health checks
            external_id: Optional external ID (e.g. CLI key)
            project_path: Optional project path
            project_id: Optional project ID
        """
        self._connections: dict[str, BaseTransportConnection] = {}
        self._configs: dict[str, MCPServerConfig] = {}
        # Changed to public health attribute to match tests
        self.health: dict[str, MCPConnectionHealth] = {}
        self._token_refresh_callback = token_refresh_callback
        self._health_check_interval = health_check_interval
        self._health_check_task: asyncio.Task | None = None
        self._auth_token: str | None = None
        self._running = False
        self.external_id = external_id
        self.project_path = project_path
        self.project_id = project_id

        if server_configs:
            for config in server_configs:
                self._configs[config.name] = config

    @property
    def connections(self) -> dict[str, BaseTransportConnection]:
        """Get active connections."""
        return self._connections

    def list_connections(self) -> list[MCPServerConfig]:
        """List active server connections."""
        return [self._configs[name] for name in self._connections.keys()]

    def get_client(self, server_name: str) -> BaseTransportConnection:
        """Get client connection by name."""
        if server_name not in self._configs:
            raise ValueError(f"Unknown MCP server: '{server_name}'")
        if server_name in self._connections:
            return self._connections[server_name]
        raise ValueError(f"Client '{server_name}' not connected")

    async def add_server(self, config: MCPServerConfig) -> None:
        """Add and connect to a server."""
        if config.name in self._configs:
            # Check if attempting to add duplicate
            # If connected, raise ValueError per test
            if config.name in self._connections and self._connections[config.name].is_connected:
                raise ValueError(f"MCP server '{config.name}' already exists")

        self._configs[config.name] = config
        # Attempt connect
        if config.enabled:
            await self._connect_server(config)

    async def remove_server(self, name: str, project_id: str | None = None) -> None:
        """Remove a server."""
        if name not in self._configs:
            raise ValueError(f"MCP server '{name}' not found")

        # Disconnect
        if name in self._connections:
            await self._connections[name].disconnect()
            del self._connections[name]

        del self._configs[name]
        if name in self.health:
            del self.health[name]

    async def get_health_report(self) -> dict[str, Any]:
        """Get async health report."""
        # Just wrap sync health report or enhance?
        # Tests expect dict result.
        # Format: {name: {state: ..., health: ...}}
        # My get_server_health returns exactly this.
        # However, test_get_health_report_with_tracking expects 'state', 'health' strings.
        # My enum values are strings ("connected", "healthy").
        return self.get_server_health()

    @property
    def server_configs(self) -> list[MCPServerConfig]:
        """Get all server configurations."""
        return list(self._configs.values())

    async def connect_all(self, configs: list[MCPServerConfig] | None = None) -> dict[str, bool]:
        """
        Connect to multiple MCP servers.

        Args:
            configs: List of server configurations. If None, uses registered configs.

        Returns:
            Dict mapping server names to success status
        """
        self._running = True
        results = {}

        configs_to_connect = configs if configs is not None else self.server_configs

        # Store configs if provided
        if configs:
            for config in configs:
                self._configs[config.name] = config

        # Initialize health tracking for all configs
        for config in self.server_configs:
            if config.name not in self.health:
                self.health[config.name] = MCPConnectionHealth(
                    name=config.name,
                    state=ConnectionState.DISCONNECTED,
                )

        # Start health check task if not running
        if self._health_check_task is None:
            self._health_check_task = asyncio.create_task(self._monitor_health())

        # Connect concurrently
        connect_tasks = []
        bound_configs = []
        for config in configs_to_connect:
            if not config.enabled:
                logger.debug(f"Skipping disabled server: {config.name}")
                results[config.name] = False
                continue

            task = asyncio.create_task(self._connect_server(config))
            connect_tasks.append(task)
            bound_configs.append(config)

        if not connect_tasks:
            return results

        task_results = await asyncio.gather(*connect_tasks, return_exceptions=True)

        for config, result in zip(bound_configs, task_results, strict=False):
            if isinstance(result, Exception):
                logger.error(f"Failed to connect to {config.name}: {result}")
                results[config.name] = False
            else:
                results[config.name] = bool(result)

        return results

    async def health_check_all(self) -> dict[str, Any]:
        """Perform immediate health check on all connections."""
        tasks = []
        server_names = []

        for name, connection in self._connections.items():
            if connection.is_connected:
                tasks.append(connection.health_check(timeout=5.0))
                server_names.append(name)

        if not tasks:
            return {}

        results = await asyncio.gather(*tasks, return_exceptions=True)

        health_status = {}
        for name, result in zip(server_names, results, strict=False):
            if isinstance(result, Exception) or result is False:
                self.health[name].record_failure("Health check failed")
                health_status[name] = False
            else:
                self.health[name].record_success()
                health_status[name] = True

        return health_status

    async def _connect_server(self, config: MCPServerConfig) -> ClientSession | None:
        """Connect to a single server."""
        try:
            # Create transport if doesn't exist or if config changed
            # (Simplification: always recreate for now if not connected)
            if config.name not in self._connections:
                connection = create_transport_connection(
                    config,
                    self._auth_token,
                    self._token_refresh_callback,
                )
                self._connections[config.name] = connection

            connection = self._connections[config.name]

            # Update health state
            self.health[config.name].state = ConnectionState.CONNECTING

            session = await connection.connect()

            # Update health state
            self.health[config.name].state = ConnectionState.CONNECTED
            self.health[config.name].record_success()

            return session

        except Exception as e:
            self.health[config.name].state = ConnectionState.FAILED
            self.health[config.name].record_failure(str(e))
            raise

    async def disconnect_all(self) -> None:
        """Disconnect all active connections."""
        self._running = False

        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
            self._health_check_task = None

        tasks = []
        for name, connection in self._connections.items():
            if connection.is_connected:
                tasks.append(asyncio.create_task(connection.disconnect()))
                self.health[name].state = ConnectionState.DISCONNECTED

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._connections.clear()

    async def get_session(self, server_name: str) -> ClientSession:
        """
        Get active session for server.

        Args:
            server_name: Name of server

        Raises:
            KeyError: If server not configured
            MCPError: If not connected
        """
        if server_name not in self._connections:
            raise KeyError(f"Server '{server_name}' not configured")

        connection = self._connections[server_name]
        if not connection.is_connected:
            # Try auto-reconnect
            try:
                await connection.connect()
            except Exception as e:
                raise MCPError(f"Server '{server_name}' disconnected and reconnect failed") from e

        # Since BaseTransportConnection.connect returns ClientSession, we can't fully access
        # a .session property directly if it wasn't exposed publically in base.
        # But we added `_session` in base and `connect` returns it.
        # However, the type hint for connect is Any.
        # Let's rely on the transport keeping a session reference.
        # We need to expose it on the base class or return it here.
        # The connection object has a `_session` attribute but we should access it safely.
        # Let's assume the subclasses implement a mechanism or we access `_session` (which is protected)
        # For this refactor, let's access the protected member or use the one returned from connect.
        # But connect is async and we just want to get it.
        # We should improve BaseTransportConnection to expose `session`.

        # Accessing protected member for now as it was in the original design (it was all one file)
        # Ideally we'd add a property.
        session = connection._session  # type: ignore
        if not session:
            raise MCPError(f"Server '{server_name}' has no active session")

        return session

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Call a tool on a specific server."""
        try:
            session = await self.get_session(server_name)
            if timeout:
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments or {}), timeout=timeout
                )
            else:
                result = await session.call_tool(tool_name, arguments or {})
            self.health[server_name].record_success()
            return result
        except Exception as e:
            if server_name in self.health:
                self.health[server_name].record_failure(str(e))
            raise

    async def read_resource(self, server_name: str, uri: str) -> Any:
        """Read a resource from a specific server."""
        try:
            session = await self.get_session(server_name)
            # Ensure uri is string and cast for type checker if needed,
            # though runtime usually handles string -> AnyUrl coercion in pydantic
            result = await session.read_resource(cast(Any, str(uri)))
            self.health[server_name].record_success()
            return result
        except Exception as e:
            if server_name in self.health:
                self.health[server_name].record_failure(str(e))
            raise

    async def list_tools(self, server_name: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """
        List tools from one or all servers.

        Args:
            server_name: Optional single server name

        Returns:
            Dict mapping server names to tool lists
        """
        results = {}
        servers = [server_name] if server_name else self._connections.keys()

        for name in servers:
            try:
                session = await self.get_session(name)
                tools = await session.list_tools()
                # Assuming tools is a ListToolsResult or similar Pydantic model
                # We need to serialize it or return it as is.
                # Inspecting mcp-python-sdk, list_tools returns ListToolsResult.
                # Let's return the raw object or access .tools
                if hasattr(tools, "tools"):
                    results[name] = tools.tools
                else:
                    results[name] = tools  # Fallback

                self.health[name].record_success()
            except Exception as e:
                logger.warning(f"Failed to list tools for {name}: {e}")
                self.health[name].record_failure(str(e))
                results[name] = []

        return results

    async def get_tool_input_schema(self, server_name: str, tool_name: str) -> dict[str, Any]:
        """Get full inputSchema for a specific tool."""

        # This is an optimization. Instead of calling list_tools again,
        # we try to fetch it. But standard MCP list_tools returns everything.
        # So we just filter the output of list_tools.

        tools = await self.list_tools(server_name)
        server_tools = tools.get(server_name, [])

        for tool in server_tools:
            # tool might be an object or dict
            t_name = getattr(tool, "name", tool.get("name") if isinstance(tool, dict) else None)
            if t_name == tool_name:
                # Return schema
                if hasattr(tool, "inputSchema"):
                    return getattr(tool, "inputSchema")
                if isinstance(tool, dict) and "inputSchema" in tool:
                    return tool["inputSchema"]

        raise MCPError(f"Tool {tool_name} not found on server {server_name}")

    async def _monitor_health(self) -> None:
        """Background task to monitor connection health."""
        while self._running:
            try:
                await asyncio.sleep(self._health_check_interval)

                tasks = []
                server_names = []

                for name, connection in self._connections.items():
                    if connection.is_connected:
                        tasks.append(connection.health_check(timeout=5.0))
                        server_names.append(name)

                if not tasks:
                    continue

                results = await asyncio.gather(*tasks, return_exceptions=True)

                for name, result in zip(server_names, results, strict=False):
                    if isinstance(result, Exception) or result is False:
                        # Health check failed
                        self.health[name].record_failure("Health check failed")
                        logger.warning(f"Health check failed for {name}")

                        # Trigger reconnect if critical
                        if self.health[name].health == HealthState.UNHEALTHY:
                            logger.info(f"Attempting reconnection for unhealthy server: {name}")
                            asyncio.create_task(self._reconnect(name))
                    else:
                        self.health[name].record_success()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in health monitor: {e}")

    async def _reconnect(self, server_name: str) -> None:
        """Attempt to reconnect a server."""
        if server_name not in self._configs:
            return

        config = self._configs[server_name]
        try:
            logger.info(f"Reconnecting {server_name}...")
            await self._connect_server(config)
            logger.info(f"Successfully reconnected {server_name}")
        except Exception as e:
            logger.error(f"Reconnection failed for {server_name}: {e}")

    def get_server_health(self) -> dict[str, dict[str, Any]]:
        """Get health status for all servers."""
        return {
            name: {
                "state": status.state.value,
                "health": status.health.value,
                "last_check": status.last_health_check.isoformat()
                if status.last_health_check
                else None,
                "failures": status.consecutive_failures,
                "response_time_ms": status.response_time_ms,
            }
            for name, status in self.health.items()
        }

    def add_server_config(self, config: MCPServerConfig) -> None:
        """Register a new server configuration."""
        self._configs[config.name] = config
        if config.name not in self.health:
            self.health[config.name] = MCPConnectionHealth(
                name=config.name, state=ConnectionState.DISCONNECTED
            )

    def remove_server_config(self, name: str) -> None:
        """Remove a server configuration."""
        if name in self._configs:
            del self._configs[name]
        if name in self._connections:
            # We should disconnect first ideally, but this is just config removal
            # The caller should ensure disconnect happens
            pass
