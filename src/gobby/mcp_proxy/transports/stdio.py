"""Stdio transport connection."""

import logging
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from mcp.client import Transport
from mcp.client.stdio import StdioServerParameters, stdio_client

from gobby.mcp_proxy.bundled import prefers_offline_npx, resolve_runtime_stdio_args
from gobby.mcp_proxy.transports.base import OwnerTaskTransportConnection
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


class StdioTransportConnection(OwnerTaskTransportConnection):
    """Stdio transport connection using MCP SDK."""

    _TRANSPORT_LABEL = "Stdio"
    _OWNER_TASK_PREFIX = "stdio"

    def __init__(
        self,
        config: "MCPServerConfig",
        stdio_errlog_path: str | None = None,
    ) -> None:
        """Initialize stdio transport connection."""
        super().__init__(config)
        self._stdio_errlog_path = stdio_errlog_path
        self._stdio_errlog_handle: TextIO | None = None

    def _open_stdio_errlog(self, stack: AsyncExitStack) -> TextIO:
        """Open the subprocess stderr sink; a file handle closes with ``stack``."""
        if self._stdio_errlog_path is None:
            return sys.stderr

        errlog_path = Path(self._stdio_errlog_path).expanduser()
        errlog_path.parent.mkdir(parents=True, exist_ok=True)
        handle = errlog_path.open("a", encoding="utf-8")
        self._stdio_errlog_handle = handle
        stack.callback(self._close_stdio_errlog, handle)
        return handle

    def _close_stdio_errlog(self, errlog_handle: TextIO) -> None:
        if errlog_handle is self._stdio_errlog_handle:
            self._stdio_errlog_handle = None
        try:
            errlog_handle.close()
        except Exception as exc:
            logger.warning("Error closing stdio errlog for %s: %s", self.config.name, exc)

    async def _open_transport(self, stack: AsyncExitStack) -> Transport:
        """Build the subprocess transport; the owner task enters and exits it."""
        if self.config.command is None:
            raise RuntimeError("Command is required for stdio transport")

        # Expand ${VAR} patterns in args and env values
        runtime_args = resolve_runtime_stdio_args(self.config.runtime_hook, self.config.args)
        expanded_args = _expand_args(runtime_args) or []
        expanded_env = expand_env_mapping(self.config.env)
        if prefers_offline_npx(self.config.command):
            expanded_env = dict(expanded_env or {})
            expanded_env.setdefault("npm_config_prefer_offline", "true")

        params = StdioServerParameters(
            command=self.config.command,
            args=expanded_args,
            env=expanded_env,
        )
        return stdio_client(params, errlog=self._open_stdio_errlog(stack))
