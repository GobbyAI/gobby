"""Drive the native ``gmcp`` wrapper over MCP stdio, the way an agent's MCP client does."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

from tests.e2e.test_runtime_boundary import _native_bin

# Terminal-context keys and the environment variables gmcp captures them from.
# gmcp takes parent_pid from its real OS parent, which is the test process here.
_TERMINAL_ENV = {
    "tmux_pane": "TMUX_PANE",
    "tmux_socket_path": "TMUX",
    "tty": "TTY",
    "term_program": "TERM_PROGRAM",
    "term_session_id": "TERM_SESSION_ID",
    "gobby_terminal_id": "GOBBY_TERMINAL_ID",
}


def terminal_env(terminal_context: Mapping[str, Any]) -> dict[str, str]:
    """Environment under which gmcp captures the terminal context the hooks send."""
    return {
        env: str(terminal_context[key])
        for key, env in _TERMINAL_ENV.items()
        if key in terminal_context
    }


class GmcpSession:
    """Proxy tools of one running gmcp process; each call returns its JSON payload."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def _payload(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._session.call_tool(tool, arguments)
        block = result.content[0]
        assert isinstance(block, TextContent), result
        payload: dict[str, Any] = json.loads(block.text)
        return payload

    async def list_tools(self, server_name: str) -> dict[str, Any]:
        return await self._payload("list_tools", {"server_name": server_name})

    async def get_tool_schema(self, server_name: str, tool_name: str) -> dict[str, Any]:
        return await self._payload(
            "get_tool_schema", {"server_name": server_name, "tool_name": tool_name}
        )

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        preflight_enabled: bool = True,
    ) -> dict[str, Any]:
        return await self._payload(
            "call_tool",
            {
                "server_name": server_name,
                "tool_name": tool_name,
                "arguments": arguments,
                "preflight_enabled": preflight_enabled,
            },
        )

    async def list_mcp_servers(self, session_id: str | None = None) -> dict[str, Any]:
        arguments = {} if session_id is None else {"session_id": session_id}
        return await self._payload("list_mcp_servers", arguments)


@asynccontextmanager
async def gmcp_session(
    home: Path,
    *,
    cwd: Path | None = None,
    daemon_url: str | None = None,
    terminal_context: Mapping[str, Any] | None = None,
    identity_env: Mapping[str, str] | None = None,
) -> AsyncIterator[GmcpSession]:
    """Start one gmcp process and open its MCP session.

    The environment is built from scratch. The MCP SDK adds only its safe defaults, so
    no tmux, TTY, or GOBBY_* variable from the test runner reaches the wrapper. HOME is
    the test home as well: if gmcp ignored GOBBY_HOME, its ~/.gobby fallback would find
    nothing here rather than the operator's daemon and key.
    """
    env = {"GOBBY_HOME": str(home), "HOME": str(home)}
    if daemon_url is not None:
        env["GOBBY_DAEMON_URL"] = daemon_url
    env.update(terminal_env(terminal_context or {}))
    env.update(identity_env or {})
    params = StdioServerParameters(
        command=str(_native_bin("gmcp")), args=[], env=env, cwd=cwd or home
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield GmcpSession(session)
