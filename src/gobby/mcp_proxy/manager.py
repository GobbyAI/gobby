"""
Manager for multiple MCP client connections.

Supports lazy initialization where servers are connected on-demand
rather than at startup, reducing resource usage and startup time.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any, cast

from mcp import ClientSession

from gobby.mcp_proxy.lazy import (
    CircuitBreakerOpen,
    LazyServerConnector,
    RetryConfig,
)
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

__all__ = [
    "MCPClientManager",
    "MCPServerConfig",
    "ConnectionState",
    "HealthState",
    "MCPConnectionHealth",
    "MCPError",
]

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
        mcp_db_manager: Any | None = None,
        lazy_connect: bool = True,
        preconnect_servers: list[str] | None = None,
        connection_timeout: float = 30.0,
        max_connection_retries: int = 3,
        metrics_manager: Any | None = None,
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
            mcp_db_manager: LocalMCPManager instance for database-backed server/tool storage.
                When provided with project_id, loads servers from the database automatically.
            lazy_connect: If True, defer connections until first use (default: True)
            preconnect_servers: List of server names to connect eagerly even in lazy mode
            connection_timeout: Timeout in seconds for connection attempts
            max_connection_retries: Maximum retry attempts for failed connections
            metrics_manager: ToolMetricsManager instance for recording call metrics
        """
        self._connections: dict[str, BaseTransportConnection] = {}
        self._configs: dict[str, MCPServerConfig] = {}
        # Changed to public health attribute to match tests
        self.health: dict[str, MCPConnectionHealth] = {}
        self._token_refresh_callback = token_refresh_callback
        self._health_check_interval = health_check_interval
        self._health_check_task: asyncio.Task[None] | None = None
        self._reconnect_tasks: set[asyncio.Task[None]] = set()
        self._auth_token: str | None = None
        self._running = False
        self.external_id = external_id
        self.project_path = project_path
        self.project_id = project_id
        self.mcp_db_manager = mcp_db_manager
        self.metrics_manager = metrics_manager

        # Lazy connection settings
        self.lazy_connect = lazy_connect
        self.preconnect_servers = set(preconnect_servers or [])
        self.connection_timeout = connection_timeout
        self.max_connection_retries = max_connection_retries

        # Initialize lazy connector with retry config
        self._lazy_connector = LazyServerConnector(
            retry_config=RetryConfig(max_retries=max_connection_retries),
        )

        # Load server configs from database if not provided explicitly
        if server_configs is None and mcp_db_manager is not None:
            if project_id:
                # Load servers for specific project
                db_servers = mcp_db_manager.list_servers(
                    project_id=project_id,
                    enabled_only=False,
                )
            else:
                # Load all servers (daemon startup)
                db_servers = mcp_db_manager.list_all_servers(enabled_only=False)

            for s in db_servers:
                config = MCPServerConfig(
                    name=s.name,
                    transport=s.transport,
                    url=s.url,
                    command=s.command,
                    args=s.args,
                    env=s.env,
                    headers=s.headers,
                    enabled=s.enabled,
                    description=s.description,
                    project_id=s.project_id,
                    tools=self._load_tools_from_db(mcp_db_manager, s.name, s.project_id),
                )
                self._configs[config.name] = config
                # Register with lazy connector for deferred connection
                self._lazy_connector.register_server(config.name)
            logger.info(f"Loaded {len(self._configs)} MCP servers from database")
        elif server_configs:
            for config in server_configs:
                self._configs[config.name] = config
                # Register with lazy connector for deferred connection
                self._lazy_connector.register_server(config.name)

    @staticmethod
    def _load_tools_from_db(
        mcp_db_manager: Any, server_name: str, project_id: str
    ) -> list[dict[str, str]] | None:
        """
        Load cached tools from database for a server.

        Returns lightweight tool metadata for MCPServerConfig.tools field.
        """
        try:
            tools = mcp_db_manager.get_cached_tools(server_name, project_id=project_id)
            if not tools:
                return None
            return [
                {
                    "name": tool.name,
                    "brief": (tool.description or "")[:100],  # Truncate to brief
                }
                for tool in tools
            ]
        except Exception as e:
            logger.warning(f"Failed to load cached tools for '{server_name}': {e}")
            return None

    @property
    def connections(self) -> dict[str, BaseTransportConnection]:
        """Get active connections."""
        return self._connections

    def list_connections(self) -> list[MCPServerConfig]:
        """List active server connections."""
        return [self._configs[name] for name in self._connections.keys()]

    def get_available_servers(self) -> list[str]:
        """Get list of available server names."""
        return list(self._configs.keys())

    def get_client(self, server_name: str) -> BaseTransportConnection:
        """Get client connection by name."""
        if server_name not in self._configs:
            raise ValueError(f"Unknown MCP server: '{server_name}'")
        if server_name in self._connections:
            return self._connections[server_name]
        raise ValueError(f"Client '{server_name}' not connected")

    def has_server(self, server_name: str) -> bool:
        """Check if server is configured and exists."""
        return server_name in self._configs

    async def add_server(self, config: MCPServerConfig) -> dict[str, Any]:
        """Add and connect to a server."""
        if config.name in self._configs:
            raise ValueError(f"MCP server '{config.name}' already exists")

        self._configs[config.name] = config

        # Persist to database if manager is available
        if self.mcp_db_manager and config.project_id:
            self.mcp_db_manager.upsert(
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
        # Attempt connect
        if config.enabled:
            session = await self._connect_server(config)
            if session:
                try:
                    tools_result = await session.list_tools()
                    # Convert Tool objects to dicts
                    for t in tools_result.tools:
                        tool_schemas.append(
                            {
                                "name": t.name,
                                "description": getattr(t, "description", "") or "",
                                "inputSchema": getattr(t, "inputSchema", {}) or {},
                            }
                        )
                except Exception as e:
                    logger.warning(f"Failed to list tools for {config.name}: {e}")

            if tool_schemas:
                self._cache_discovered_tools(config.name, tool_schemas)

        return {
            "success": True,
            "name": config.name,
            "full_tool_schemas": tool_schemas,
        }

    async def remove_server(self, name: str, project_id: str | None = None) -> dict[str, Any]:
        """Remove a server."""
        if name not in self._configs:
            raise ValueError(f"MCP server '{name}' not found")

        # Get project_id from config if not provided
        config = self._configs[name]
        effective_project_id = project_id or config.project_id

        # Disconnect
        if name in self._connections:
            await self._connections[name].disconnect()
            del self._connections[name]

        del self._configs[name]
        if name in self.health:
            del self.health[name]

        # Remove from database if manager is available
        if self.mcp_db_manager and effective_project_id:
            self.mcp_db_manager.remove_server(name, effective_project_id)

        return {"success": True, "name": name}

    async def get_health_report(self) -> dict[str, Any]:
        """Get async health report."""
        return self.get_server_health()

    @property
    def server_configs(self) -> list[MCPServerConfig]:
        """Get all server configurations."""
        return list(self._configs.values())

    async def connect_all(self, configs: list[MCPServerConfig] | None = None) -> dict[str, bool]:
        """
        Connect to multiple MCP servers.

        In lazy mode (default), only connects servers in preconnect_servers list.
        In eager mode (lazy_connect=False), connects all enabled servers.

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
                self._lazy_connector.register_server(config.name)

        # Initialize health tracking for all configs
        for config in self.server_configs:
            if config.name not in self.health:
                # Lazy servers that won't be preconnected start as PENDING
                if self.lazy_connect and config.name not in self.preconnect_servers:
                    initial_state = ConnectionState.PENDING
                else:
                    initial_state = ConnectionState.DISCONNECTED
                self.health[config.name] = MCPConnectionHealth(
                    name=config.name,
                    state=initial_state,
                )

        # Start health check task if not running
        if self._health_check_task is None:
            self._health_check_task = asyncio.create_task(self._monitor_health())

        # In lazy mode, only connect preconnect servers
        if self.lazy_connect:
            configs_to_connect = [
                c for c in configs_to_connect if c.name in self.preconnect_servers
            ]
            if configs_to_connect:
                logger.info(
                    f"Lazy mode: preconnecting {len(configs_to_connect)} servers "
                    f"({', '.join(c.name for c in configs_to_connect)})"
                )
            else:
                logger.info(
                    f"Lazy mode: no preconnect servers configured, "
                    f"{len(self._configs)} servers available on-demand"
                )

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
                if result:
                    self._lazy_connector.mark_connected(config.name)

        return results

    def get_lazy_connection_states(self) -> dict[str, dict[str, Any]]:
        """
        Get lazy connection states for all registered servers.

        Returns:
            Dict mapping server names to connection state info including:
            - is_connected: Whether server is connected
            - configured_at: When server was configured
            - connected_at: When server was connected (if connected)
            - last_error: Last error message (if any)
            - circuit_state: Circuit breaker state (closed/open/half_open)
        """
        return self._lazy_connector.get_all_states()

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

    def _resolve_secrets_in_config(self, config: MCPServerConfig) -> MCPServerConfig:
        """Resolve $secret:NAME references in config headers and env.

        Returns a copy with secrets resolved, leaving the original unchanged.
        """
        if not config.headers and not config.env:
            return config

        try:
            from gobby.storage.secrets import SECRET_REF_PATTERN, SecretStore

            has_refs = False
            for d in (config.headers, config.env):
                if d:
                    for v in d.values():
                        if SECRET_REF_PATTERN.search(v):
                            has_refs = True
                            break

            if not has_refs:
                return config

            # Get database from mcp_db_manager
            db = getattr(self.mcp_db_manager, "db", None) if self.mcp_db_manager else None
            if not db:
                return config

            store = SecretStore(db)
            import dataclasses

            updates: dict[str, Any] = {}
            if config.headers:
                updates["headers"] = store.resolve_dict(config.headers)
            if config.env:
                updates["env"] = store.resolve_dict(config.env)
            return dataclasses.replace(config, **updates)
        except Exception as e:
            logger.debug(f"Secret resolution skipped for {config.name}: {e}")
            return config

    async def _connect_server(self, config: MCPServerConfig) -> ClientSession | None:
        """Connect to a single server."""
        # Ensure health record exists before we try to update it
        if config.name not in self.health:
            self.health[config.name] = MCPConnectionHealth(
                name=config.name, state=ConnectionState.DISCONNECTED
            )

        try:
            # Resolve $secret:NAME references in headers/env before connecting
            resolved_config = self._resolve_secrets_in_config(config)

            # Create transport if doesn't exist or if config changed
            # (Simplification: always recreate for now if not connected)
            if config.name not in self._connections:
                connection = create_transport_connection(
                    resolved_config,
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

            return cast(ClientSession | None, session)

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

        # Cancel any pending reconnect tasks
        for task in list(self._reconnect_tasks):
            task.cancel()
        if self._reconnect_tasks:
            await asyncio.gather(*self._reconnect_tasks, return_exceptions=True)
        self._reconnect_tasks.clear()

        async def disconnect_with_timeout(name: str, connection: Any) -> None:
            try:
                await asyncio.wait_for(connection.disconnect(), timeout=5.0)
            except TimeoutError:
                logger.warning(f"Connection disconnect timed out for {name}")
            except Exception as e:
                logger.warning(f"Error disconnecting {name}: {e}")

        tasks = []
        for name, connection in self._connections.items():
            if connection.is_connected:
                tasks.append(asyncio.create_task(disconnect_with_timeout(name, connection)))
                self.health[name].state = ConnectionState.DISCONNECTED

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._connections.clear()

    async def ensure_connected(self, server_name: str) -> ClientSession:
        """
        Ensure a server is connected, connecting lazily if needed.

        This is the main entry point for lazy connection. It handles:
        - First-time connection for unconfigured servers
        - Reconnection for disconnected servers
        - Circuit breaker protection against repeated failures
        - Exponential backoff retry on connection failure

        Args:
            server_name: Name of server to connect

        Returns:
            Active ClientSession for the server

        Raises:
            KeyError: If server not configured
            CircuitBreakerOpen: If circuit breaker is open (too many failures)
            MCPError: If connection fails after retries
        """
        if server_name not in self._configs:
            raise KeyError(f"Server '{server_name}' not configured")

        config = self._configs[server_name]

        # Check if server is disabled
        if not config.enabled:
            raise MCPError(f"Server '{server_name}' is disabled")

        # Check if already connected
        if server_name in self._connections:
            connection = self._connections[server_name]
            if connection.is_connected and connection.session:
                return connection.session

        # Check circuit breaker
        if not self._lazy_connector.can_attempt_connection(server_name):
            state = self._lazy_connector.get_state(server_name)
            if state and state.circuit_breaker.last_failure_time:
                elapsed = time.time() - state.circuit_breaker.last_failure_time
                recovery_in = max(0, state.circuit_breaker.recovery_timeout - elapsed)
                raise CircuitBreakerOpen(server_name, recovery_in)
            raise MCPError(f"Circuit breaker open for '{server_name}'")

        # Use lock to prevent concurrent connection attempts
        async with self._lazy_connector.get_connection_lock(server_name):
            # Double-check after acquiring lock
            if server_name in self._connections:
                connection = self._connections[server_name]
                if connection.is_connected and connection.session:
                    return connection.session

            # Attempt connection with retry
            retry_config = self._lazy_connector.retry_config
            last_error: Exception | None = None

            for attempt in range(retry_config.max_retries + 1):
                try:
                    state = self._lazy_connector.get_state(server_name)
                    if state:
                        state.record_connection_attempt()

                    session = await asyncio.wait_for(
                        self._connect_server(config),
                        timeout=self.connection_timeout,
                    )

                    if session:
                        self._lazy_connector.mark_connected(server_name)
                        return session
                    else:
                        raise MCPError(f"Connection returned no session for '{server_name}'")

                except TimeoutError:
                    last_error = MCPError(f"Connection timeout after {self.connection_timeout}s")
                    self._lazy_connector.mark_failed(server_name, str(last_error))
                except Exception as e:
                    last_error = e
                    self._lazy_connector.mark_failed(server_name, str(e))

                # If not last attempt, wait with exponential backoff
                if attempt < retry_config.max_retries:
                    delay = retry_config.get_delay(attempt)
                    logger.warning(
                        f"Connection to '{server_name}' failed (attempt {attempt + 1}/"
                        f"{retry_config.max_retries + 1}), retrying in {delay:.1f}s: {last_error}"
                    )
                    await asyncio.sleep(delay)

            # All retries exhausted
            raise MCPError(
                f"Failed to connect to '{server_name}' after "
                f"{retry_config.max_retries + 1} attempts: {last_error}"
            ) from last_error

    async def get_client_session(self, server_name: str) -> ClientSession:
        """
        Get active MCP client session for server, connecting lazily if needed.

        Args:
            server_name: Name of server

        Returns:
            Active ClientSession

        Raises:
            KeyError: If server not configured
            MCPError: If not connected and connection fails
        """
        # Use ensure_connected for lazy connection
        return await self.ensure_connected(server_name)

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Call a tool on a specific server."""
        start_time = time.perf_counter()
        success = False
        try:
            session = await self.get_client_session(server_name)
            if timeout:
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments or {}), timeout=timeout
                )
            else:
                result = await session.call_tool(tool_name, arguments or {})
            self.health[server_name].record_success()
            success = True
            return result
        except Exception as e:
            if server_name in self.health:
                self.health[server_name].record_failure(str(e))
            raise
        finally:
            # Record metrics if manager is configured
            if self.metrics_manager:
                latency_ms = (time.perf_counter() - start_time) * 1000
                # Get project_id from server config (servers are project-scoped)
                server_config = self._configs.get(server_name)
                metrics_project_id = server_config.project_id if server_config else self.project_id
                if metrics_project_id:
                    try:
                        self.metrics_manager.record_call(
                            server_name=server_name,
                            tool_name=tool_name,
                            project_id=metrics_project_id,
                            latency_ms=latency_ms,
                            success=success,
                        )
                    except Exception:
                        # Don't let metrics recording failures affect tool calls
                        logger.debug(f"Failed to record metrics for {server_name}.{tool_name}")

    async def read_resource(self, server_name: str, uri: str) -> Any:
        """Read a resource from a specific server."""
        try:
            session = await self.get_client_session(server_name)
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
                session = await self.get_client_session(name)
                tools = await session.list_tools()
                # Assuming tools is a ListToolsResult or similar Pydantic model
                # We need to serialize it or return it as is.
                # Inspecting mcp-python-sdk, list_tools returns ListToolsResult.
                # Let's return the raw object or access .tools
                if hasattr(tools, "tools"):
                    tool_list = [
                        {
                            "name": t.name,
                            "description": getattr(t, "description", "") or "",
                            "inputSchema": getattr(t, "inputSchema", {}) or {},
                        }
                        for t in tools.tools
                    ]
                    results[name] = tool_list
                    self._cache_discovered_tools(name, tool_list)
                else:
                    results[name] = []

                self.health[name].record_success()
            except Exception as e:
                logger.warning(f"Failed to list tools for {name}: {e}")
                self.health[name].record_failure(str(e))
                results[name] = []

        return results

    def _cache_discovered_tools(self, server_name: str, tools: list[dict[str, Any]]) -> None:
        """Cache discovered tools to DB and update in-memory config."""
        config = self._configs.get(server_name)
        if not config or not self.mcp_db_manager or not config.project_id:
            return

        try:
            self.mcp_db_manager.cache_tools(server_name, tools, project_id=config.project_id)
            config.tools = [
                {"name": t["name"], "brief": (t.get("description", "") or "")[:100]} for t in tools
            ]
        except Exception as e:
            logger.debug(f"Failed to cache tools for {server_name}: {e}")

    async def get_tool_input_schema(self, server_name: str, tool_name: str) -> dict[str, Any]:
        """Get full inputSchema for a specific tool."""
        tool_info = await self.get_tool_info(server_name, tool_name)
        input_schema = tool_info.get("inputSchema", {})
        return cast(dict[str, Any], input_schema)

    async def get_tool_info(self, server_name: str, tool_name: str) -> dict[str, Any]:
        """Get full tool info including name, description, and inputSchema."""

        # This is an optimization. Instead of calling list_tools again,
        # we try to fetch it. But standard MCP list_tools returns everything.
        # So we just filter the output of list_tools.

        tools = await self.list_tools(server_name)
        server_tools = tools.get(server_name, [])

        for tool in server_tools:
            # tool might be an object or dict
            t_name = getattr(tool, "name", tool.get("name") if isinstance(tool, dict) else None)
            if t_name == tool_name:
                if isinstance(tool, dict):
                    result: dict[str, Any] = {"name": t_name}
                    if "description" in tool and tool["description"]:
                        result["description"] = tool["description"]
                    if "inputSchema" in tool:
                        result["inputSchema"] = tool["inputSchema"]
                    return result

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
                            task = asyncio.create_task(self._reconnect(name))
                            self._reconnect_tasks.add(task)
                            task.add_done_callback(self._reconnect_tasks.discard)
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
                "last_check": (
                    status.last_health_check.isoformat() if status.last_health_check else None
                ),
                "failures": status.consecutive_failures,
                "response_time_ms": status.response_time_ms,
            }
            for name, status in self.health.items()
        }

    def add_server_config(self, config: MCPServerConfig) -> None:
        """Register a new server configuration."""
        self._configs[config.name] = config
        if config.name not in self.health:
            initial_state = (
                ConnectionState.PENDING if self.lazy_connect else ConnectionState.DISCONNECTED
            )
            self.health[config.name] = MCPConnectionHealth(name=config.name, state=initial_state)

    def remove_server_config(self, name: str) -> None:
        """Remove a server configuration.

        Raises RuntimeError if a connection exists for the server,
        forcing callers to disconnect first.
        """
        if name in self._connections:
            # Raise instead of async cleanup to keep this method synchronous.
            # Callers must explicitly disconnect before removing config.
            logger.warning(
                f"Removing config for '{name}' but connection still exists. "
                "You should disconnect the server first."
            )
            raise RuntimeError(
                f"Cannot remove config for connected server '{name}'. Disconnect it first."
            )

        if name in self._configs:
            del self._configs[name]
