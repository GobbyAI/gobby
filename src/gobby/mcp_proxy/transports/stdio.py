"""Stdio transport connection."""

import asyncio
import logging
import os
import re
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from gobby.mcp_proxy.models import ConnectionState, MCPError
from gobby.mcp_proxy.transports.base import BaseTransportConnection

# Pattern for ${VAR} or ${VAR:-default} environment variable expansion
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env_var(value: str) -> str:
    """Expand ${VAR} and ${VAR:-default} patterns in a string.

    Args:
        value: String that may contain ${VAR} patterns

    Returns:
        String with environment variables expanded
    """

    def replace_match(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default_value = match.group(2)  # None if no default specified

        env_value = os.environ.get(var_name)

        if env_value is not None and env_value != "":
            return env_value
        elif default_value is not None:
            return default_value
        else:
            # Leave unchanged if no value and no default
            return match.group(0)

    return ENV_VAR_PATTERN.sub(replace_match, value)


def _expand_env_dict(env: dict[str, str] | None) -> dict[str, str] | None:
    """Expand environment variables in env dict values.

    Args:
        env: Dictionary of environment variables (may contain ${VAR} patterns)

    Returns:
        Dictionary with expanded values, or None if input is None
    """
    if env is None:
        return None

    return {key: _expand_env_var(value) for key, value in env.items()}


def _expand_args(args: list[str] | None) -> list[str] | None:
    """Expand environment variables in command args.

    Args:
        args: List of command arguments (may contain ${VAR} patterns)

    Returns:
        List with expanded values, or None if input is None
    """
    if args is None:
        return None

    return [_expand_env_var(arg) for arg in args]


if TYPE_CHECKING:
    from gobby.mcp_proxy.models import MCPServerConfig

logger = logging.getLogger("gobby.mcp.client")


class StdioTransportConnection(BaseTransportConnection):
    """Stdio transport connection using MCP SDK."""

    def __init__(
        self,
        config: "MCPServerConfig",
        auth_token: str | None = None,
        token_refresh_callback: Callable[[], Coroutine[Any, Any, str]] | None = None,
    ) -> None:
        """Initialize stdio transport connection."""
        super().__init__(config, auth_token, token_refresh_callback)
        self._session_context: ClientSession | None = None
        # Explicitly initialize transport context (inherited from base class, but
        # ensures the attribute exists with proper type annotation for this transport)
        self._transport_context: Any | None = None

    async def connect(self) -> Any:
        """Connect via stdio transport."""
        if self._state == ConnectionState.CONNECTED:
            return self._session

        self._state = ConnectionState.CONNECTING

        # Track what was entered for cleanup
        transport_entered = False
        session_entered = False

        try:
            # Create stdio server parameters
            if self.config.command is None:
                raise RuntimeError("Command is required for stdio transport")

            # Expand ${VAR} patterns in args and env values
            expanded_args = _expand_args(self.config.args) or []
            expanded_env = _expand_env_dict(self.config.env)

            params = StdioServerParameters(
                command=self.config.command,
                args=expanded_args,
                env=expanded_env,
            )

            # Create stdio client context
            self._transport_context = stdio_client(params)

            # Enter the transport context to get streams
            read_stream, write_stream = await self._transport_context.__aenter__()
            transport_entered = True

            # Save the context manager itself so we can call __aexit__ on it later
            self._session_context = ClientSession(read_stream, write_stream)
            self._session = await self._session_context.__aenter__()
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
            session_ctx = self._session_context
            if session_entered and session_ctx is not None:
                try:
                    await session_ctx.__aexit__(None, None, None)
                except Exception as cleanup_error:
                    logger.warning(
                        f"Error during session cleanup for {self.config.name}: {cleanup_error}"
                    )

            transport_ctx = self._transport_context
            if transport_entered and transport_ctx is not None:
                try:
                    await transport_ctx.__aexit__(None, None, None)
                except Exception as cleanup_error:
                    logger.warning(
                        f"Error during transport cleanup for {self.config.name}: {cleanup_error}"
                    )

            # Reset state before raising
            self._session = None
            self._session_context = None
            self._transport_context = None
            self._state = ConnectionState.FAILED

            # Re-raise wrapped in MCPError (don't double-wrap)
            if isinstance(e, MCPError):
                raise
            raise MCPError(f"Stdio connection failed: {error_msg}") from e

    async def disconnect(self) -> None:
        """Disconnect from stdio server."""
        # Exit session context manager (not the session object itself)
        session_ctx = self._session_context
        if session_ctx is not None:
            try:
                await asyncio.wait_for(session_ctx.__aexit__(None, None, None), timeout=2.0)
            except TimeoutError:
                logger.warning(f"Session close timed out for {self.config.name}")
            except RuntimeError as e:
                # Expected when exiting cancel scope from different task
                if "cancel scope" not in str(e):
                    logger.warning(f"Error closing session for {self.config.name}: {e}")
            except Exception as e:
                logger.warning(f"Error closing session for {self.config.name}: {e}")
            self._session_context = None
            self._session = None

        transport_ctx = self._transport_context
        if transport_ctx is not None:
            try:
                await asyncio.wait_for(transport_ctx.__aexit__(None, None, None), timeout=2.0)
            except TimeoutError:
                logger.warning(f"Transport close timed out for {self.config.name}")
            except RuntimeError as e:
                # Expected when exiting cancel scope from different task
                if "cancel scope" not in str(e):
                    logger.warning(f"Error closing transport for {self.config.name}: {e}")
            except Exception as e:
                logger.warning(f"Error closing transport for {self.config.name}: {e}")
            self._transport_context = None

        self._state = ConnectionState.DISCONNECTED
