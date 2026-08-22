"""Base transport connection abstract class."""

import asyncio
import logging
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Any

from mcp.client import Client, ClientSession, Transport

from gobby.mcp_proxy.models import ConnectionState, MCPError, MCPServerConfig

logger = logging.getLogger("gobby.mcp.client")

_HEALTH_ERROR_MAX_LENGTH = 500


def _format_health_error(error: Exception) -> str:
    message = " ".join(str(error).split())
    detail = f"{type(error).__name__}: {message}" if message else type(error).__name__
    return detail[:_HEALTH_ERROR_MAX_LENGTH]


class BaseTransportConnection:
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
    ):
        """
        Initialize transport connection.

        Args:
            config: Server configuration
        """
        self.config = config
        self._session: Any | None = None  # ClientSession
        self._client_context: Client | None = None  # Owns transport + session lifecycle
        self._state = ConnectionState.DISCONNECTED
        self._last_health_check: datetime | None = None
        self._last_health_error: str | None = None
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

    @property
    def session(self) -> ClientSession | None:
        """Get the current client session, if connected."""
        return self._session

    @property
    def last_health_error(self) -> str | None:
        """Get the most recent health-check failure detail."""
        return self._last_health_error

    async def health_check(self, timeout: float = 5.0) -> bool:
        """
        Check connection health.

        Args:
            timeout: Health check timeout in seconds

        Returns:
            True if healthy, False otherwise
        """
        if not self.is_connected or not self._session:
            self._last_health_error = "Connection is not active"
            return False

        try:
            # Use asyncio.wait_for for timeout
            await asyncio.wait_for(self._session.list_tools(), timeout)
            self._last_health_check = datetime.now(UTC)
            self._last_health_error = None
            self._consecutive_failures = 0
            return True
        except TimeoutError:
            self._consecutive_failures += 1
            self._last_health_error = f"list_tools timed out after {timeout:g}s"
            return False
        except Exception as error:
            self._consecutive_failures += 1
            self._last_health_error = _format_health_error(error)
            return False


class OwnerTaskTransportConnection(BaseTransportConnection):
    """Transport whose ``Client`` is entered and exited by one dedicated task.

    Every SDK transport (stdio subprocess, Streamable HTTP, SSE, and the custom
    WebSocket task group) holds anyio cancel scopes that must be exited from
    the task that entered them. The manager connects from per-server tasks and
    disconnects from another, so an owner task holds the whole lifecycle and
    ``disconnect()`` only signals it and waits.
    """

    _OWNER_TASK_SHUTDOWN_TIMEOUT = 2.0
    _TRANSPORT_LABEL = "transport"
    _OWNER_TASK_PREFIX = "transport"

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._owner_task: asyncio.Task[None] | None = None
        self._disconnect_event: asyncio.Event | None = None
        self._session_ready: asyncio.Event | None = None
        self._connection_error: Exception | None = None

    async def _open_transport(self, stack: AsyncExitStack) -> Transport:
        """Build the SDK transport; resources it needs are owned by ``stack``."""
        raise NotImplementedError

    async def connect(self) -> Any:
        """Connect through a dedicated owner task and return the session."""
        if self._state == ConnectionState.CONNECTED and self._session is not None:
            return self._session

        # Clean up old connection if reconnecting
        if self._owner_task is not None:
            await self.disconnect()

        self._state = ConnectionState.CONNECTING
        self._connection_error = None

        self._disconnect_event = asyncio.Event()
        self._session_ready = asyncio.Event()

        self._owner_task = asyncio.create_task(
            self._run_connection(), name=f"{self._OWNER_TASK_PREFIX}-conn-{self.config.name}"
        )

        timeout = self.config.connect_timeout
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=timeout)
        except TimeoutError as e:
            await self._abandon_connect_attempt(ConnectionState.FAILED)
            raise MCPError(f"Connection timeout for {self.config.name} after {timeout}s") from e
        except asyncio.CancelledError:
            # The caller gave up mid-handshake; the owner task must not outlive it.
            await self._abandon_connect_attempt(ConnectionState.DISCONNECTED)
            raise

        if self._connection_error is not None:
            error = self._connection_error
            self._connection_error = None
            await self._cleanup_owner_task()
            self._state = ConnectionState.FAILED
            raise error

        if self._session is None:
            # The owner task ended (e.g. cancelled) without publishing a session.
            await self._cleanup_owner_task()
            self._state = ConnectionState.FAILED
            raise MCPError(
                f"{self._TRANSPORT_LABEL} connection for {self.config.name} ended "
                "before a session was established"
            )

        return self._session

    async def _abandon_connect_attempt(self, final_state: ConnectionState) -> None:
        """Cancel an owner task that never published a session and wait for it."""
        if self._disconnect_event is not None:
            self._disconnect_event.set()
        if self._owner_task is not None and not self._owner_task.done():
            self._owner_task.cancel()
        await self._cleanup_owner_task()
        self._state = final_state

    async def _run_connection(self) -> None:
        """Owner task: enter the transport and Client, hold them until disconnect."""
        if self._disconnect_event is None or self._session_ready is None:
            raise RuntimeError("Connection events not initialized")

        connected = False
        try:
            async with AsyncExitStack() as stack:
                transport = await self._open_transport(stack)
                # Client negotiates the protocol era (server/discover with an
                # initialize fallback) before publishing the session.
                self._client_context = Client(transport)
                async with self._client_context as client:
                    self._session = client.session

                    self._state = ConnectionState.CONNECTED
                    self._consecutive_failures = 0
                    logger.debug(
                        "Connected to %s MCP server: %s",
                        self._TRANSPORT_LABEL,
                        self.config.name,
                    )

                    connected = True
                    self._session_ready.set()

                    await self._disconnect_event.wait()

                    logger.debug("Disconnect requested for %s", self.config.name)

        except Exception as e:
            error_msg = str(e) if str(e) else f"{type(e).__name__}: Connection closed or timed out"
            if connected:
                # The session was live; this is a teardown failure, and the
                # caller is already unwinding.
                logger.warning(
                    "Error closing %s client for %s: %s",
                    self._TRANSPORT_LABEL,
                    self.config.name,
                    error_msg,
                )
                return
            logger.error(
                "Failed to connect to %s server '%s': %s",
                self._TRANSPORT_LABEL,
                self.config.name,
                error_msg,
                extra={"server": self.config.name, "error_type": type(e).__name__},
            )

            if isinstance(e, MCPError):
                self._connection_error = e
            else:
                self._connection_error = MCPError(
                    f"{self._TRANSPORT_LABEL} connection failed: {error_msg}"
                )

            self._session_ready.set()  # Unblock waiter with error

        finally:
            self._session = None
            self._client_context = None
            self._state = ConnectionState.DISCONNECTED
            # A cancelled or otherwise aborted handshake must still release
            # a waiting connect() call.
            if self._session_ready is not None:
                self._session_ready.set()

    async def _cleanup_owner_task(self) -> None:
        """Wait for the owner task to unwind, cancelling it if it stalls."""
        if self._owner_task is not None:
            if not self._owner_task.done():
                done, _pending = await asyncio.wait(
                    {self._owner_task},
                    timeout=self._OWNER_TASK_SHUTDOWN_TIMEOUT,
                )
                if done:
                    try:
                        await self._owner_task
                    except asyncio.CancelledError:
                        logger.debug("Owner task cancelled for %s", self.config.name)
                    except Exception as e:
                        logger.warning("Owner task cleanup failed for %s: %s", self.config.name, e)
                else:
                    self._owner_task.cancel()
                    try:
                        await asyncio.wait_for(
                            self._owner_task,
                            timeout=self._OWNER_TASK_SHUTDOWN_TIMEOUT,
                        )
                    except asyncio.CancelledError:
                        logger.debug("Owner task cancelled for %s", self.config.name)
                    except TimeoutError:
                        logger.warning("Owner task cleanup timed out for %s", self.config.name)
                    except Exception as e:
                        logger.warning("Owner task cleanup failed for %s: %s", self.config.name, e)
            self._owner_task = None
        self._disconnect_event = None
        self._session_ready = None

    async def disconnect(self) -> None:
        """Signal the owner task to unwind the Client and wait for it."""
        if self._disconnect_event is not None:
            self._disconnect_event.set()

        await self._cleanup_owner_task()
        self._state = ConnectionState.DISCONNECTED
