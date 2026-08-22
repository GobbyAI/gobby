"""Stdio transport connection."""

import asyncio
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from gobby.mcp_proxy.bundled import (
    is_bundled_external_mcp_server,
    resolve_runtime_stdio_args,
)
from gobby.mcp_proxy.models import ConnectionState, MCPError
from gobby.mcp_proxy.transports.base import BaseTransportConnection
from gobby.utils.env import expand_env_mapping, expand_env_variables


def _expand_args(args: list[str] | None) -> list[str] | None:
    """Expand environment variables in command args.

    Args:
        args: List of command arguments (may contain ${VAR} patterns)

    Returns:
        List with expanded values, or None if input is None
    """
    if args is None:
        return None

    return [expand_env_variables(arg) for arg in args]


if TYPE_CHECKING:
    from gobby.mcp_proxy.models import MCPServerConfig

logger = logging.getLogger("gobby.mcp.client")


class StdioTransportConnection(BaseTransportConnection):
    """Stdio transport connection using MCP SDK."""

    def __init__(
        self,
        config: "MCPServerConfig",
        stdio_errlog_path: str | None = None,
    ) -> None:
        """Initialize stdio transport connection."""
        super().__init__(config)
        self._stdio_errlog_path = stdio_errlog_path
        self._stdio_errlog_handle: TextIO | None = None

    def _open_stdio_errlog(self) -> TextIO:
        if self._stdio_errlog_path is None:
            return sys.stderr

        errlog_path = Path(self._stdio_errlog_path).expanduser()
        errlog_path.parent.mkdir(parents=True, exist_ok=True)
        self._stdio_errlog_handle = errlog_path.open("a", encoding="utf-8")
        return self._stdio_errlog_handle

    def _close_stdio_errlog(self, errlog_handle: TextIO | None = None) -> None:
        if errlog_handle is None:
            errlog_handle = self._stdio_errlog_handle
            self._stdio_errlog_handle = None
        elif errlog_handle is self._stdio_errlog_handle:
            self._stdio_errlog_handle = None
        if errlog_handle is not None:
            try:
                errlog_handle.close()
            except Exception as exc:
                logger.warning("Error closing stdio errlog for %s: %s", self.config.name, exc)

    async def _cleanup_connect_attempt(self, *, client_entered: bool) -> None:
        """Release a partially established connection after failure or cancellation.

        ``Client.__aenter__`` unwinds the transport itself when the handshake
        fails, so only a fully entered client needs an explicit exit here.
        """
        client_ctx = self._client_context
        self._session = None
        self._client_context = None
        self._state = ConnectionState.DISCONNECTED
        cancelled_error: asyncio.CancelledError | None = None
        try:
            if client_entered and client_ctx is not None:
                try:
                    await asyncio.wait_for(client_ctx.__aexit__(None, None, None), timeout=2.0)
                except TimeoutError:
                    logger.warning(
                        "Client cleanup timed out for %s",
                        self.config.name,
                        extra={"server": self.config.name, "cleanup_stage": "client"},
                    )
                except asyncio.CancelledError as exc:
                    logger.warning(
                        "Client cleanup cancelled for %s",
                        self.config.name,
                        extra={"server": self.config.name, "cleanup_stage": "client"},
                    )
                    cancelled_error = exc
                except Exception as cleanup_error:
                    logger.warning(
                        "Error during client cleanup for %s: %s",
                        self.config.name,
                        cleanup_error,
                        extra={"server": self.config.name, "cleanup_stage": "client"},
                    )
        finally:
            self._close_stdio_errlog()
        if cancelled_error is not None:
            raise cancelled_error

    async def connect(self) -> Any:
        """Connect via stdio transport."""
        if self._state == ConnectionState.CONNECTED:
            return self._session

        self._state = ConnectionState.CONNECTING
        client_entered = False

        try:
            # Create stdio server parameters
            if self.config.command is None:
                raise RuntimeError("Command is required for stdio transport")

            # Expand ${VAR} patterns in args and env values
            runtime_args = resolve_runtime_stdio_args(self.config.name, self.config.args)
            expanded_args = _expand_args(runtime_args) or []
            expanded_env = expand_env_mapping(self.config.env)
            if self.config.command == "npx" and is_bundled_external_mcp_server(self.config.name):
                expanded_env = dict(expanded_env or {})
                expanded_env.setdefault("npm_config_prefer_offline", "true")

            params = StdioServerParameters(
                command=self.config.command,
                args=expanded_args,
                env=expanded_env,
            )

            # Client owns the subprocess transport and the session, and
            # negotiates the protocol era (server/discover, then initialize).
            errlog = self._open_stdio_errlog()
            self._client_context = Client(stdio_client(params, errlog=errlog))
            client = await self._client_context.__aenter__()
            client_entered = True
            self._session = client.session

            self._state = ConnectionState.CONNECTED
            self._consecutive_failures = 0
            logger.debug("Connected to stdio MCP server: %s", self.config.name)

            return self._session

        except asyncio.CancelledError:
            await self._cleanup_connect_attempt(client_entered=client_entered)
            self._state = ConnectionState.DISCONNECTED
            raise
        except Exception as e:
            # Handle exceptions with empty str() (EndOfStream, ClosedResourceError)
            error_msg = str(e) if str(e) else f"{type(e).__name__}: Connection closed or timed out"
            logger.error(
                "Failed to connect to stdio server %s: %s",
                self.config.name,
                error_msg,
                extra={"server": self.config.name, "error_type": type(e).__name__},
            )

            await self._cleanup_connect_attempt(client_entered=client_entered)
            self._state = ConnectionState.FAILED

            # Re-raise wrapped in MCPError (don't double-wrap)
            if isinstance(e, MCPError):
                raise
            raise MCPError(f"Stdio connection failed: {error_msg}") from e

    async def disconnect(self) -> None:
        """Disconnect from stdio server."""
        cancelled_error: asyncio.CancelledError | None = None
        errlog_handle = self._stdio_errlog_handle
        client_ctx = self._client_context
        if client_ctx is not None:
            try:
                await asyncio.wait_for(client_ctx.__aexit__(None, None, None), timeout=2.0)
            except TimeoutError:
                logger.warning("Client close timed out for %s", self.config.name)
            except asyncio.CancelledError as exc:
                logger.warning("Client close cancelled for %s", self.config.name)
                cancelled_error = exc
            except RuntimeError as e:
                # Expected when exiting cancel scope from different task
                if "cancel scope" not in str(e):
                    logger.warning("Error closing client for %s: %s", self.config.name, e)
            except Exception as e:
                logger.warning("Error closing client for %s: %s", self.config.name, e)
            self._client_context = None
            self._session = None

        self._close_stdio_errlog(errlog_handle)
        self._state = ConnectionState.DISCONNECTED
        if cancelled_error is not None:
            raise cancelled_error
