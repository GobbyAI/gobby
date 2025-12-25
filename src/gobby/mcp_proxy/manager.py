"""
MCP Client Manager for multi-server connection pooling.

Manages multiple MCP server connections with shared authentication,
health monitoring, concurrent operations, and automatic token refresh.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

# Use the dedicated MCP client logger for connection and proxy operations
# This logger writes to the mcp_client log file configured in config.yaml
logger = logging.getLogger("gobby.mcp.client")


class ConnectionState(str, Enum):
    """MCP connection state."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"


class MCPError(Exception):
    """Base exception for MCP client errors."""

    def __init__(self, message: str, code: int | None = None):
        """
        Initialize MCP error.

        Args:
            message: Error message
            code: JSON-RPC error code (if applicable)
        """
        super().__init__(message)
        self.code = code


class HealthState(str, Enum):
    """Connection health state for monitoring."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class MCPConnectionHealth:
    """
    Health tracking for individual MCP connection.

    Tracks connection state, consecutive failures, and last health check
    to enable health monitoring and automatic recovery.
    """

    name: str
    state: ConnectionState
    health: HealthState = HealthState.HEALTHY
    last_health_check: datetime | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    response_time_ms: float | None = None

    def record_success(self, response_time_ms: float | None = None) -> None:
        """
        Record successful operation.

        Args:
            response_time_ms: Response time in milliseconds (optional)
        """
        self.consecutive_failures = 0
        self.last_error = None
        self.health = HealthState.HEALTHY
        self.response_time_ms = response_time_ms
        self.last_health_check = datetime.now()

    def record_failure(self, error: str) -> None:
        """
        Record failed operation and update health state.

        Args:
            error: Error message from failure
        """
        self.consecutive_failures += 1
        self.last_error = error
        self.last_health_check = datetime.now()

        # Update health state based on failure count
        if self.consecutive_failures >= 5:
            self.health = HealthState.UNHEALTHY
        elif self.consecutive_failures >= 3:
            self.health = HealthState.DEGRADED


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server with transport support."""

    name: str
    enabled: bool = True

    # Transport configuration
    transport: str = "http"  # "http", "stdio", "websocket", "sse"

    # HTTP/WebSocket/SSE transport
    url: str | None = None
    headers: dict[str, str] | None = None  # Custom headers (e.g., API keys)

    # Stdio transport
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None

    # OAuth/Auth (for HTTP/WebSocket)
    requires_oauth: bool = False
    oauth_provider: str | None = None  # e.g., "google", "github"

    # Tool metadata (cached summaries)
    tools: list[dict[str, str]] | None = None  # [{"name": "tool_name", "description": "..."}]

    # Server description (what it does, when to use it)
    description: str | None = None

    # Project context
    project_id: str = ""  # Required - UUID string for the project this server belongs to

    def validate(self) -> None:
        """Validate configuration based on transport type."""
        if self.transport in ("http", "websocket", "sse"):
            if not self.url:
                raise ValueError(f"{self.transport} transport requires 'url' parameter")
        elif self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio transport requires 'command' parameter")
        else:
            raise ValueError(f"Unsupported transport: {self.transport}")


# ===== TRANSPORT-SPECIFIC CONNECTION CLASSES =====


class _BaseTransportConnection:
    """
    Base class for MCP transport connections.

    All transport implementations must provide:
    - connect() -> ClientSession
    - disconnect()
    - is_connected property
    - state property
    """

    def __init__(
        self,
        config: MCPServerConfig,
        auth_token: str | None = None,
        token_refresh_callback: Callable[[], Coroutine[Any, Any, str]] | None = None,
    ):
        """
        Initialize transport connection.

        Args:
            config: Server configuration
            auth_token: Optional auth token
            token_refresh_callback: Optional callback for token refresh
        """
        self.config = config
        self._auth_token = auth_token
        self._token_refresh_callback = token_refresh_callback
        self._session: Any | None = None  # ClientSession
        self._transport_context: Any | None = None  # Transport-specific context manager
        self._state = ConnectionState.DISCONNECTED
        self._last_health_check: datetime | None = None
        self._consecutive_failures = 0

    async def connect(self) -> Any:
        """Connect and return ClientSession. Must be implemented by subclasses."""
        raise NotImplementedError

    async def disconnect(self) -> None:
        """Disconnect from server. Must be implemented by subclasses."""
        raise NotImplementedError

    @property
    def is_connected(self) -> bool:
        """Check if connection is active."""
        return self._state == ConnectionState.CONNECTED and self._session is not None

    @property
    def state(self) -> ConnectionState:
        """Get current connection state."""
        return self._state

    def set_auth_token(self, token: str) -> None:
        """Update authentication token."""
        self._auth_token = token

    async def health_check(self, timeout: float = 5.0) -> bool:
        """
        Check connection health.

        Args:
            timeout: Health check timeout in seconds

        Returns:
            True if healthy, False otherwise
        """
        if not self.is_connected or not self._session:
            return False

        try:
            # Use asyncio.wait_for for timeout
            await asyncio.wait_for(self._session.list_tools(), timeout)
            self._last_health_check = datetime.now()
            self._consecutive_failures = 0
            return True
        except (TimeoutError, Exception):
            self._consecutive_failures += 1
            return False


class _HTTPTransportConnection(_BaseTransportConnection):
    """HTTP/Streamable HTTP transport connection using MCP SDK."""

    async def connect(self) -> Any:
        """Connect via HTTP transport."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        if self._state == ConnectionState.CONNECTED and self._session is not None:
            return self._session

        # Clean up old connection if reconnecting
        if self._session is not None or self._transport_context is not None:
            await self.disconnect()

        self._state = ConnectionState.CONNECTING

        # Track what was entered for cleanup
        transport_entered = False
        session_entered = False
        session_context: ClientSession | None = None

        try:
            # URL is required for HTTP transport
            assert self.config.url is not None, "URL is required for HTTP transport"

            # Create HTTP client context with custom headers
            self._transport_context = streamablehttp_client(
                self.config.url,
                headers=self.config.headers,  # Pass custom headers (e.g., API keys)
            )

            # Enter the transport context to get streams
            read_stream, write_stream, _ = await self._transport_context.__aenter__()
            transport_entered = True

            session_context = ClientSession(read_stream, write_stream)
            self._session = await session_context.__aenter__()
            session_entered = True

            await self._session.initialize()

            self._state = ConnectionState.CONNECTED
            self._consecutive_failures = 0
            logger.debug(f"Connected to HTTP MCP server: {self.config.name}")

            return self._session

        except Exception as e:
            # Handle exceptions with empty str() (EndOfStream, ClosedResourceError, CancelledError)
            error_msg = str(e) if str(e) else f"{type(e).__name__}: Connection closed or timed out"
            logger.error(f"Failed to connect to HTTP server '{self.config.name}': {error_msg}")

            # Cleanup in reverse order - session first, then transport
            if session_entered and session_context is not None:
                try:
                    await session_context.__aexit__(None, None, None)
                except Exception as cleanup_error:
                    logger.warning(
                        f"Error during session cleanup for {self.config.name}: {cleanup_error}"
                    )

            if transport_entered and self._transport_context is not None:
                try:
                    await self._transport_context.__aexit__(None, None, None)
                except Exception as cleanup_error:
                    logger.warning(
                        f"Error during transport cleanup for {self.config.name}: {cleanup_error}"
                    )

            # Reset state before raising
            self._session = None
            self._transport_context = None
            self._state = ConnectionState.FAILED

            # Re-raise wrapped in MCPError (don't double-wrap)
            if isinstance(e, MCPError):
                raise
            raise MCPError(f"HTTP connection failed: {error_msg}") from e

    async def disconnect(self) -> None:
        """Disconnect from HTTP server."""
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except RuntimeError as e:
                # Expected when exiting cancel scope from different task
                if "cancel scope" not in str(e):
                    logger.warning(f"Error closing session for {self.config.name}: {e}")
            except Exception as e:
                logger.warning(f"Error closing session for {self.config.name}: {e}")
            self._session = None

        if self._transport_context is not None:
            try:
                await self._transport_context.__aexit__(None, None, None)
            except RuntimeError as e:
                # Expected when exiting cancel scope from different task
                if "cancel scope" not in str(e):
                    logger.warning(f"Error closing transport for {self.config.name}: {e}")
            except Exception as e:
                logger.warning(f"Error closing transport for {self.config.name}: {e}")
            self._transport_context = None

        self._state = ConnectionState.DISCONNECTED


class _StdioTransportConnection(_BaseTransportConnection):
    """Stdio transport connection using MCP SDK."""

    async def connect(self) -> Any:
        """Connect via stdio transport."""
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        if self._state == ConnectionState.CONNECTED:
            return self._session

        self._state = ConnectionState.CONNECTING

        # Track what was entered for cleanup
        transport_entered = False
        session_entered = False
        session_context: ClientSession | None = None

        try:
            # Create stdio server parameters
            assert self.config.command is not None, "Command is required for stdio transport"
            params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args or [],
                env=self.config.env,
            )

            # Create stdio client context
            self._transport_context = stdio_client(params)

            # Enter the transport context to get streams
            read_stream, write_stream = await self._transport_context.__aenter__()
            transport_entered = True

            session_context = ClientSession(read_stream, write_stream)
            self._session = await session_context.__aenter__()
            session_entered = True

            await self._session.initialize()

            self._state = ConnectionState.CONNECTED
            self._consecutive_failures = 0
            logger.debug(f"Connected to stdio MCP server: {self.config.name}")

            return self._session

        except Exception as e:
            # Handle exceptions with empty str() (EndOfStream, ClosedResourceError, CancelledError)
            error_msg = str(e) if str(e) else f"{type(e).__name__}: Connection closed or timed out"
            logger.error(f"Failed to connect to stdio server '{self.config.name}': {error_msg}")

            # Cleanup in reverse order - session first, then transport
            if session_entered and session_context is not None:
                try:
                    await session_context.__aexit__(None, None, None)
                except Exception as cleanup_error:
                    logger.warning(
                        f"Error during session cleanup for {self.config.name}: {cleanup_error}"
                    )

            if transport_entered and self._transport_context is not None:
                try:
                    await self._transport_context.__aexit__(None, None, None)
                except Exception as cleanup_error:
                    logger.warning(
                        f"Error during transport cleanup for {self.config.name}: {cleanup_error}"
                    )

            # Reset state before raising
            self._session = None
            self._transport_context = None
            self._state = ConnectionState.FAILED

            # Re-raise wrapped in MCPError (don't double-wrap)
            if isinstance(e, MCPError):
                raise
            raise MCPError(f"Stdio connection failed: {error_msg}") from e

    async def disconnect(self) -> None:
        """Disconnect from stdio server."""
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except RuntimeError as e:
                # Expected when exiting cancel scope from different task
                if "cancel scope" not in str(e):
                    logger.warning(f"Error closing session for {self.config.name}: {e}")
            except Exception as e:
                logger.warning(f"Error closing session for {self.config.name}: {e}")
            self._session = None

        if self._transport_context is not None:
            try:
                await self._transport_context.__aexit__(None, None, None)
            except RuntimeError as e:
                # Expected when exiting cancel scope from different task
                if "cancel scope" not in str(e):
                    logger.warning(f"Error closing transport for {self.config.name}: {e}")
            except Exception as e:
                logger.warning(f"Error closing transport for {self.config.name}: {e}")
            self._transport_context = None

        self._state = ConnectionState.DISCONNECTED


class _WebSocketTransportConnection(_BaseTransportConnection):
    """WebSocket transport connection using MCP SDK."""

    async def connect(self) -> Any:
        """Connect via WebSocket transport."""
        from mcp import ClientSession
        from mcp.client.websocket import websocket_client

        if self._state == ConnectionState.CONNECTED:
            return self._session

        self._state = ConnectionState.CONNECTING

        # Track what was entered for cleanup
        transport_entered = False
        session_entered = False
        session_context: ClientSession | None = None

        try:
            # URL is required for WebSocket transport
            assert self.config.url is not None, "URL is required for WebSocket transport"

            # Create WebSocket client context
            self._transport_context = websocket_client(self.config.url)

            # Enter the transport context to get streams
            read_stream, write_stream = await self._transport_context.__aenter__()
            transport_entered = True

            session_context = ClientSession(read_stream, write_stream)
            self._session = await session_context.__aenter__()
            session_entered = True

            await self._session.initialize()

            self._state = ConnectionState.CONNECTED
            self._consecutive_failures = 0
            logger.debug(f"Connected to WebSocket MCP server: {self.config.name}")

            return self._session

        except Exception as e:
            # Handle exceptions with empty str() (EndOfStream, ClosedResourceError, CancelledError)
            error_msg = str(e) if str(e) else f"{type(e).__name__}: Connection closed or timed out"
            logger.error(f"Failed to connect to WebSocket server '{self.config.name}': {error_msg}")

            # Cleanup in reverse order - session first, then transport
            if session_entered and session_context is not None:
                try:
                    await session_context.__aexit__(None, None, None)
                except Exception as cleanup_error:
                    logger.warning(
                        f"Error during session cleanup for {self.config.name}: {cleanup_error}"
                    )

            if transport_entered and self._transport_context is not None:
                try:
                    await self._transport_context.__aexit__(None, None, None)
                except Exception as cleanup_error:
                    logger.warning(
                        f"Error during transport cleanup for {self.config.name}: {cleanup_error}"
                    )

            # Reset state before raising
            self._session = None
            self._transport_context = None
            self._state = ConnectionState.FAILED

            # Re-raise wrapped in MCPError (don't double-wrap)
            if isinstance(e, MCPError):
                raise
            raise MCPError(f"WebSocket connection failed: {error_msg}") from e

    async def disconnect(self) -> None:
        """Disconnect from WebSocket server."""
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except RuntimeError as e:
                # Expected when exiting cancel scope from different task
                if "cancel scope" not in str(e):
                    logger.warning(f"Error closing session for {self.config.name}: {e}")
            except Exception as e:
                logger.warning(f"Error closing session for {self.config.name}: {e}")
            self._session = None

        if self._transport_context is not None:
            try:
                await self._transport_context.__aexit__(None, None, None)
            except RuntimeError as e:
                # Expected when exiting cancel scope from different task
                if "cancel scope" not in str(e):
                    logger.warning(f"Error closing transport for {self.config.name}: {e}")
            except Exception as e:
                logger.warning(f"Error closing transport for {self.config.name}: {e}")
            self._transport_context = None

        self._state = ConnectionState.DISCONNECTED


def _create_transport_connection(
    config: MCPServerConfig,
    auth_token: str | None = None,
    token_refresh_callback: Callable[[], Coroutine[Any, Any, str]] | None = None,
) -> _BaseTransportConnection:
    """
    Factory function to create appropriate transport connection.

    Args:
        config: Server configuration
        auth_token: Optional auth token
        token_refresh_callback: Optional token refresh callback

    Returns:
        Transport-specific connection instance

    Raises:
        ValueError: If transport type is unsupported
    """
    transport_map = {
        "http": _HTTPTransportConnection,
        "stdio": _StdioTransportConnection,
        "websocket": _WebSocketTransportConnection,
    }

    transport_class = transport_map.get(config.transport)
    if not transport_class:
        raise ValueError(
            f"Unsupported transport: {config.transport}. Supported: {list(transport_map.keys())}"
        )

    return transport_class(config, auth_token, token_refresh_callback)


class MCPClientManager:
    """
    Manages multiple MCP client connections with shared authentication.

    Provides connection pooling, health monitoring, concurrent operations,
    and automatic token refresh across all MCP servers (Memory, Tasks, Agents, Tools).

    Example:
        ```python
        configs = [
            MCPServerConfig(name="context7", url="https://mcp.context7.com/mcp"),
            MCPServerConfig(name="supabase", url="http://localhost:6543/mcp"),
        ]

        async with MCPClientManager(configs) as manager:
            # Call specific MCP
            result = await manager.call_tool("context7", "get-library-docs", {...})

            # Check health
            health_status = await manager.health_check_all()
        ```
    """

    def __init__(
        self,
        server_configs: list[MCPServerConfig] | None = None,
        external_id: str = "",
        project_path: str | None = None,
        project_id: str | None = None,
        token_refresh_callback: Callable[[], Coroutine[Any, Any, str]] | None = None,
        mcp_db_manager: Any | None = None,
    ):
        """
        Initialize MCP client manager.

        Args:
            server_configs: List of MCP server configurations (optional if mcp_db_manager provided)
            external_id: Unique session identifier
            project_path: Project root path (optional)
            project_id: Project ID for scoped server filtering (optional).
                       When set, loads project-specific servers + global servers,
                       with project servers taking precedence on name conflicts.
            token_refresh_callback: Async function that returns new auth token
            mcp_db_manager: LocalMCPManager instance for database-backed server/tool storage
        """
        self.project_id = project_id

        # Load server configs from database if not provided
        if server_configs is None:
            if mcp_db_manager is not None and project_id:
                # Load servers for this project
                db_servers = mcp_db_manager.list_servers(
                    project_id=project_id,
                    enabled_only=False,
                )
                self.server_configs = [
                    MCPServerConfig(
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
                        # Load cached tools from database
                        tools=self._load_tools_from_db(mcp_db_manager, s.name, s.project_id),
                    )
                    for s in db_servers
                ]
                logger.info(
                    f"Loaded {len(self.server_configs)} MCP servers from database (project {project_id})"
                )
            else:
                self.server_configs = []
                logger.warning("No server configs or mcp_db_manager provided")
        else:
            self.server_configs = server_configs

        self.external_id = external_id
        self.project_path = project_path
        self.token_refresh_callback = token_refresh_callback
        self.mcp_db_manager = mcp_db_manager

        # Connection pool: {name: _BaseTransportConnection}
        self.connections: dict[str, _BaseTransportConnection] = {}

        # Health tracking: {name: MCPConnectionHealth}
        self.health: dict[str, MCPConnectionHealth] = {}

        # Health monitoring task
        self._health_monitor_task: asyncio.Task | None = None
        self._health_check_interval = 60  # seconds

    @staticmethod
    def _load_tools_from_db(
        mcp_db_manager: Any, server_name: str, project_id: str
    ) -> list[dict[str, str]] | None:
        """
        Load cached tools from database for a server.

        Returns lightweight tool metadata for MCPServerConfig.tools field.
        """
        from gobby.tools.filesystem import generate_brief

        try:
            tools = mcp_db_manager.get_cached_tools(server_name, project_id=project_id)
            if not tools:
                return None
            return [
                {
                    "name": tool.name,
                    "brief": generate_brief(tool.description),
                }
                for tool in tools
            ]
        except Exception as e:
            logger.warning(f"Failed to load cached tools for '{server_name}': {e}")
            return None

    async def __aenter__(self) -> "MCPClientManager":
        """
        Async context manager entry.

        Connects to all enabled MCP servers concurrently and starts
        health monitoring.

        Returns:
            MCPClientManager instance with established connections
        """
        await self.connect_all()

        # Start background health monitoring
        self._health_monitor_task = asyncio.create_task(self._health_monitor_loop())

        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """
        Async context manager exit.

        Stops health monitoring and disconnects all MCP servers.

        Args:
            exc_type: Exception type
            exc_val: Exception value
            exc_tb: Exception traceback
        """
        # Cancel health monitoring
        if self._health_monitor_task:
            self._health_monitor_task.cancel()
            try:
                await self._health_monitor_task
            except asyncio.CancelledError:
                pass

        # Disconnect all clients
        await self.disconnect_all()

    async def connect_all(self) -> None:
        """
        Connect to all enabled MCP servers concurrently.

        Uses asyncio.gather() for parallel connection establishment.
        Supports multiple transports (HTTP, stdio, WebSocket).
        Logs errors but continues connecting to other servers if one fails.
        """
        enabled_configs = [config for config in self.server_configs if config.enabled]

        if not enabled_configs:
            logger.warning("No enabled MCP servers configured")
            return

        async def connect_one(config: MCPServerConfig) -> None:
            """Connect to a single MCP server using appropriate transport."""
            try:
                # Validate configuration
                config.validate()

                # Create transport-specific connection
                connection = _create_transport_connection(
                    config=config,
                    token_refresh_callback=self.token_refresh_callback,
                )

                # Connect (returns ClientSession)
                await connection.connect()

                # Store connection and initialize health tracking
                self.connections[config.name] = connection
                self.health[config.name] = MCPConnectionHealth(
                    name=config.name,
                    state=connection.state,
                )

                logger.info(f"✓ Connected to {config.transport} MCP server: {config.name}")

            except Exception as e:
                logger.error(f"✗ Failed to connect to MCP server '{config.name}': {e}")
                self.health[config.name] = MCPConnectionHealth(
                    name=config.name,
                    state=ConnectionState.FAILED,
                    health=HealthState.UNHEALTHY,
                    last_error=str(e),
                )

        # Connect all servers concurrently
        tasks = [connect_one(config) for config in enabled_configs]
        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info(f"Connected to {len(self.connections)}/{len(enabled_configs)} MCP servers")

    async def disconnect_all(self) -> None:
        """
        Disconnect from all MCP servers concurrently.

        Uses asyncio.gather() with return_exceptions=True to ensure
        all disconnections complete even if some fail.
        """
        tasks = [client.disconnect() for client in self.connections.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

        self.connections.clear()
        logger.info("Disconnected from all MCP servers")

    async def add_server(self, config: MCPServerConfig) -> dict[str, Any]:
        """
        Dynamically add MCP server connection.

        Args:
            config: Server configuration

        Returns:
            Dict with success status and server info

        Raises:
            ValueError: If server name already exists or config is invalid
        """
        if config.name in self.connections:
            raise ValueError(f"MCP server '{config.name}' already exists")

        try:
            # Validate configuration
            config.validate()

            # Create transport-specific connection
            connection = _create_transport_connection(
                config=config,
                token_refresh_callback=self.token_refresh_callback,
            )

            # Connect
            await connection.connect()

            # Fetch tool summaries and cache to database
            full_tool_schemas = []
            if connection.is_connected and connection._session:
                try:
                    from gobby.tools.filesystem import generate_brief
                    from gobby.tools.summarizer import summarize_tools

                    tools_result = await connection._session.list_tools()
                    # Use intelligent summarization for tool descriptions
                    full_tool_schemas = await summarize_tools(tools_result.tools)

                    # Store lightweight metadata in config (just name + brief)
                    config.tools = [
                        {
                            "name": str(tool.get("name", "")),
                            "brief": generate_brief(str(tool.get("description", ""))),
                        }
                        for tool in full_tool_schemas
                        if tool.get("name")
                    ]
                    logger.info(
                        f"✓ Fetched {len(config.tools or [])} tool summaries for '{config.name}'"
                    )
                except Exception as tool_fetch_error:
                    logger.warning(
                        f"Failed to fetch tool summaries for '{config.name}': {tool_fetch_error}"
                    )
                    config.tools = []

            # Store connection and initialize health tracking
            self.connections[config.name] = connection
            self.health[config.name] = MCPConnectionHealth(
                name=config.name,
                state=connection.state,
            )

            # Add to server_configs list
            self.server_configs.append(config)

            # Persist to database if mcp_db_manager is available
            if self.mcp_db_manager is not None:
                try:
                    # Upsert server to database
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
                    logger.info(
                        f"✓ Persisted MCP server '{config.name}' to database (project {config.project_id})"
                    )

                    # Cache tools to database
                    if full_tool_schemas:
                        tool_count = self.mcp_db_manager.cache_tools(
                            config.name, full_tool_schemas, project_id=config.project_id
                        )
                        logger.info(f"✓ Cached {tool_count} tools for '{config.name}' in database")
                except Exception as persist_error:
                    logger.warning(
                        f"Failed to persist MCP server '{config.name}' to database: {persist_error}"
                    )

            logger.info(f"✓ Added {config.transport} MCP server: {config.name}")

            return {
                "success": True,
                "name": config.name,
                "transport": config.transport,
                "state": connection.state.value,
                "message": f"Successfully added and connected to '{config.name}'",
                "full_tool_schemas": full_tool_schemas,  # Full schemas for database sync
            }

        except Exception as e:
            # Some exceptions (EndOfStream, ClosedResourceError, CancelledError) have empty str()
            error_msg = str(e) if str(e) else f"{type(e).__name__}: Connection closed or timed out"
            logger.error(f"✗ Failed to add MCP server '{config.name}': {error_msg}")

            # Track failure even if connection didn't succeed
            self.health[config.name] = MCPConnectionHealth(
                name=config.name,
                state=ConnectionState.FAILED,
                health=HealthState.UNHEALTHY,
                last_error=error_msg,
            )

            return {
                "success": False,
                "name": config.name,
                "error": error_msg,
                "message": f"Failed to add server '{config.name}': {error_msg}",
            }

    async def remove_server(self, name: str, project_id: str) -> dict[str, Any]:
        """
        Remove MCP server completely.

        Performs complete cleanup:
        1. Disconnects the active connection (gracefully)
        2. Removes from in-memory connections dict
        3. Removes from health tracking dict
        4. Removes from server_configs list
        5. Removes from database (server + tools cascade deleted)

        Args:
            name: Server name to remove
            project_id: Required project ID

        Returns:
            Dict with success status and removal info

        Raises:
            ValueError: If server name not found in config
        """
        # Check if server exists in config (match by name and project_id)
        server_config = next(
            (s for s in self.server_configs if s.name == name and s.project_id == project_id), None
        )
        if not server_config:
            available = ", ".join(s.name for s in self.server_configs)
            raise ValueError(
                f"MCP server '{name}' (project {project_id}) not found in config. Available: [{available}]"
            )

        try:
            # Get transport type for response
            transport = server_config.transport

            # 1. Disconnect the active connection if it exists
            connection = self.connections.get(name)
            if connection:
                try:
                    logger.debug(f"Disconnecting MCP server '{name}'...")
                    await connection.disconnect()
                    logger.info(f"✓ Disconnected MCP server '{name}'")
                except Exception as disconnect_error:
                    # Log warning but continue with removal
                    logger.warning(
                        f"Failed to gracefully disconnect '{name}': {disconnect_error}. "
                        "Continuing with removal..."
                    )

            # 2. Remove from connections dict
            if name in self.connections:
                del self.connections[name]
                logger.debug(f"✓ Removed '{name}' from connections dict")

            # 3. Remove from health tracking dict
            if name in self.health:
                del self.health[name]
                logger.debug(f"✓ Removed '{name}' from health tracking")

            # 4. Remove from server_configs list (match by name and project_id)
            self.server_configs = [
                s
                for s in self.server_configs
                if not (s.name == name and s.project_id == project_id)
            ]

            # 5. Remove from database if mcp_db_manager is available (cascades to tools)
            if self.mcp_db_manager is not None:
                try:
                    removed = self.mcp_db_manager.remove_server(name, project_id=project_id)
                    if removed:
                        logger.info(
                            f"✓ Removed MCP server '{name}' (project {project_id}) from database"
                        )
                    else:
                        logger.debug(f"Server '{name}' (project {project_id}) was not in database")
                except Exception as persist_error:
                    logger.warning(
                        f"Failed to remove MCP server '{name}' from database: {persist_error}"
                    )

            logger.info(
                f"✓ Removed MCP server '{name}' (project {project_id}) (disconnected and removed from database)"
            )

            return {
                "success": True,
                "name": name,
                "transport": transport,
                "message": f"Successfully removed '{name}' (disconnected and removed from database)",
            }

        except Exception as e:
            logger.error(f"✗ Failed to remove MCP server '{name}': {e}")
            return {
                "success": False,
                "name": name,
                "error": str(e),
                "message": f"Failed to remove server '{name}': {e}",
            }

    def list_connections(self) -> list[str]:
        """
        Get list of connected MCP server names.

        Returns:
            List of MCP server names
        """
        return list(self.connections.keys())

    def get_client(self, mcp_name: str) -> _BaseTransportConnection:
        """
        Get MCP connection by name.

        Args:
            mcp_name: MCP server name

        Returns:
            _BaseTransportConnection instance

        Raises:
            ValueError: If MCP server name not found
        """
        connection = self.connections.get(mcp_name)
        if not connection:
            available = ", ".join(self.connections.keys())
            raise ValueError(f"Unknown MCP server: '{mcp_name}'. Available: [{available}]")
        return connection

    async def set_auth_token(self, token: str) -> None:
        """
        Update authentication token for ALL MCP connections.

        This is called when token is refreshed to ensure all clients
        use the new token.

        Args:
            token: New Bearer token from auth manager
        """
        for client in self.connections.values():
            client.set_auth_token(token)

        logger.debug(f"Updated auth token for {len(self.connections)} MCP clients")

    async def call_tool(
        self, mcp_name: str, tool_name: str, args: dict[str, Any], timeout: float | None = None
    ) -> Any:
        """
        Route tool call to specific MCP server with automatic reconnection.

        Args:
            mcp_name: MCP server name (e.g., "memory", "tasks", "agents", "tools")
            tool_name: Tool name to call
            args: Tool arguments
            timeout: Optional timeout in seconds for the tool call

        Returns:
            Result from tool call

        Raises:
            ValueError: If MCP server name not found
            MCPError: If tool call fails
            asyncio.TimeoutError: If timeout is exceeded
        """
        # Get connection for this MCP
        connection = self.get_client(mcp_name)

        # Auto-reconnect if disconnected
        if not connection.is_connected or not connection._session:
            logger.info(f"MCP server '{mcp_name}' disconnected, attempting reconnect...")
            try:
                await connection.connect()
                logger.info(f"✓ Reconnected to '{mcp_name}'")
            except Exception as e:
                raise MCPError(
                    f"MCP server '{mcp_name}' is not connected and reconnect failed: {e}"
                ) from e

        # Make the tool call using MCP SDK ClientSession
        start_time = datetime.now()
        try:
            # Session must be initialized after connect
            assert connection._session is not None, "Session not initialized"

            # Call tool via ClientSession with optional timeout
            if timeout is not None:
                result = await asyncio.wait_for(
                    connection._session.call_tool(tool_name, args), timeout=timeout
                )
            else:
                result = await connection._session.call_tool(tool_name, args)

            # Record success
            elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000
            self.health[mcp_name].record_success(response_time_ms=elapsed_ms)

            return result

        except Exception as e:
            # Check if this is a connection error that should trigger reconnect
            from anyio import ClosedResourceError

            if isinstance(e, ClosedResourceError):
                logger.warning(f"Connection to '{mcp_name}' closed, attempting reconnect...")
                try:
                    # Mark as disconnected and reconnect
                    connection._state = ConnectionState.DISCONNECTED
                    await connection.connect()
                    logger.info(f"✓ Reconnected to '{mcp_name}', retrying tool call...")

                    # Retry the tool call once after reconnect (with timeout if specified)
                    assert connection._session is not None, (
                        "Session not initialized after reconnect"
                    )
                    if timeout is not None:
                        result = await asyncio.wait_for(
                            connection._session.call_tool(tool_name, args), timeout=timeout
                        )
                    else:
                        result = await connection._session.call_tool(tool_name, args)
                    elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000
                    self.health[mcp_name].record_success(response_time_ms=elapsed_ms)
                    return result
                except Exception as reconnect_error:
                    logger.error(f"Reconnect and retry failed for '{mcp_name}': {reconnect_error}")
                    self.health[mcp_name].record_failure(str(reconnect_error))
                    self.health[mcp_name].state = connection.state
                    raise MCPError(
                        f"Tool call failed after reconnect for '{mcp_name}.{tool_name}': {reconnect_error}"
                    ) from reconnect_error

            # Record failure
            self.health[mcp_name].record_failure(str(e))
            self.health[mcp_name].state = connection.state
            raise MCPError(f"Tool call failed for '{mcp_name}.{tool_name}': {e}") from e

    async def read_resource(self, mcp_name: str, resource_uri: str) -> Any:
        """
        Read a resource from specific MCP server with automatic reconnection.

        Args:
            mcp_name: MCP server name
            resource_uri: URI of the resource to read

        Returns:
            Resource content

        Raises:
            ValueError: If MCP server name not found
            MCPError: If resource read fails
        """
        # Get connection for this MCP
        connection = self.get_client(mcp_name)

        # Auto-reconnect if disconnected
        if not connection.is_connected or not connection._session:
            logger.info(f"MCP server '{mcp_name}' disconnected, attempting reconnect...")
            try:
                await connection.connect()
                logger.info(f"✓ Reconnected to '{mcp_name}'")
            except Exception as e:
                raise MCPError(
                    f"MCP server '{mcp_name}' is not connected and reconnect failed: {e}"
                ) from e

        # Read resource using MCP SDK ClientSession
        start_time = datetime.now()
        try:
            # Session must be initialized after connect
            assert connection._session is not None, "Session not initialized"

            # Read resource via ClientSession
            resource = await connection._session.read_resource(resource_uri)

            # Record success
            elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000
            self.health[mcp_name].record_success(response_time_ms=elapsed_ms)

            return resource

        except Exception as e:
            # Record failure
            self.health[mcp_name].record_failure(str(e))
            self.health[mcp_name].state = connection.state
            raise MCPError(f"Resource read failed for '{mcp_name}/{resource_uri}': {e}") from e

    async def health_check_all(self) -> dict[str, bool]:
        """
        Check health of all MCP connections concurrently.

        Uses asyncio.gather() for parallel health checks.

        Returns:
            Dictionary mapping MCP names to health status (True = healthy)
        """

        async def check_one(name: str, client: _BaseTransportConnection) -> tuple[str, bool]:
            """Check health of a single MCP connection."""
            try:
                is_healthy = await client.health_check()
                self.health[name].state = client.state

                if is_healthy:
                    self.health[name].record_success()
                else:
                    self.health[name].record_failure("Health check failed")

                return (name, is_healthy)

            except Exception as e:
                self.health[name].record_failure(str(e))
                self.health[name].state = ConnectionState.FAILED
                return (name, False)

        # Health check all connections concurrently
        tasks = [check_one(name, client) for name, client in self.connections.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Build results dictionary
        health_status = {}
        for result in results:
            if isinstance(result, tuple):
                name, is_healthy = result
                health_status[name] = is_healthy
            else:
                # Exception occurred
                logger.error(f"Health check exception: {result}")

        return health_status

    async def get_health_report(self) -> dict[str, dict[str, Any]]:
        """
        Get comprehensive health report for all connections.

        Returns:
            Dictionary with health details for each MCP server
        """
        return {
            name: {
                "state": health.state.value,
                "health": health.health.value,
                "consecutive_failures": health.consecutive_failures,
                "last_health_check": (
                    health.last_health_check.isoformat() if health.last_health_check else None
                ),
                "last_error": health.last_error,
                "response_time_ms": health.response_time_ms,
            }
            for name, health in self.health.items()
        }

    async def reconnect(self, mcp_name: str) -> bool:
        """
        Attempt to reconnect to a specific MCP server.

        Args:
            mcp_name: MCP server name

        Returns:
            True if reconnection successful, False otherwise
        """
        client = self.get_client(mcp_name)

        try:
            # Disconnect first if connected
            if client.is_connected:
                await client.disconnect()

            # Reconnect
            await client.connect()
            success = client.is_connected

            if success:
                self.health[mcp_name].state = client.state
                self.health[mcp_name].record_success()
                logger.info(f"Successfully reconnected to MCP server: {mcp_name}")
            else:
                self.health[mcp_name].state = ConnectionState.FAILED
                self.health[mcp_name].record_failure("Reconnection failed")
                logger.error(f"Failed to reconnect to MCP server: {mcp_name}")

            return success

        except Exception as e:
            self.health[mcp_name].record_failure(str(e))
            self.health[mcp_name].state = ConnectionState.FAILED
            logger.error(f"Reconnection exception for '{mcp_name}': {e}")
            return False

    async def _health_monitor_loop(self) -> None:
        """
        Background task for periodic health monitoring.

        Runs health checks every _health_check_interval seconds
        and triggers reconnection for unhealthy connections.
        """
        while True:
            try:
                await asyncio.sleep(self._health_check_interval)

                # Run health checks
                health_status = await self.health_check_all()

                # Log summary
                healthy_count = sum(1 for is_healthy in health_status.values() if is_healthy)
                total_count = len(health_status)

                logger.info(f"Health check: {healthy_count}/{total_count} MCP servers healthy")

                # Trigger reconnection for unhealthy connections
                for name, is_healthy in health_status.items():
                    if not is_healthy and self.health[name].health == HealthState.UNHEALTHY:
                        logger.warning(
                            f"MCP server '{name}' is unhealthy "
                            f"({self.health[name].consecutive_failures} consecutive failures), "
                            f"attempting reconnection..."
                        )
                        await self.reconnect(name)

            except asyncio.CancelledError:
                logger.info("Health monitor loop cancelled")
                break
            except Exception as e:
                logger.error(f"Health monitor loop error: {e}")
