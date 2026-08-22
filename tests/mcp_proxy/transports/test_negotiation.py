"""Protocol-era negotiation through the real MCP 2.0 ``Client``.

These tests run the SDK ``Client`` unmocked against in-memory peers: a modern
``MCPServer`` that answers ``server/discover`` and a wire-level legacy server
that only speaks the ``initialize`` handshake. Both must end up as a usable
manager-facing ``ClientSession`` with list/call working and snake_case SDK
fields populated. The stdio tests spawn a real subprocess and tear it down
from a different task than the one that connected, as the manager does.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest
from mcp.client import Client, ClientSession
from mcp.types import CallToolResult, ListToolsResult
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

from gobby.mcp_proxy.client_manager.tool_inventory import list_tools_from_session
from gobby.mcp_proxy.models import ConnectionState, MCPServerConfig
from gobby.mcp_proxy.transports.stdio import StdioTransportConnection
from gobby.mcp_proxy.transports.websocket import WebSocketTransportConnection
from tests.mcp_proxy.transports._support import (
    LEGACY_PROTOCOL_VERSION,
    LEGACY_TOOL_SCHEMA,
    legacy_transport,
    modern_server,
)

pytestmark = pytest.mark.unit


async def _assert_session_usable(session: ClientSession) -> None:
    tools = await session.list_tools()
    assert isinstance(tools, ListToolsResult)
    assert [tool.name for tool in tools.tools] == ["echo"]
    assert tools.tools[0].input_schema["type"] == "object"

    result = await session.call_tool("echo", {"text": "ping"})
    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    assert result.content[0].text == "ping"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_modern_server_negotiates_via_discover() -> None:
    async with Client(modern_server()) as client:
        assert client.protocol_version in MODERN_PROTOCOL_VERSIONS
        assert client.session.discover_result is not None
        assert client.session.initialize_result is None
        assert client.server_info is not None and client.server_info.version == "9.9.9"
        await _assert_session_usable(client.session)


@pytest.mark.asyncio
async def test_legacy_server_falls_back_to_initialize() -> None:
    async with Client(legacy_transport()) as client:
        assert client.protocol_version == LEGACY_PROTOCOL_VERSION
        assert client.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS
        assert client.session.discover_result is None
        assert client.session.initialize_result is not None
        assert client.server_info is not None and client.server_info.name == "legacy"
        await _assert_session_usable(client.session)


@pytest.mark.asyncio
async def test_manager_inventory_reads_snake_case_sdk_fields_from_legacy_wire() -> None:
    """The legacy wire sends ``inputSchema``; the SDK exposes ``input_schema``."""
    async with Client(legacy_transport()) as client:
        inventory = await list_tools_from_session(client.session)

    assert inventory == [{"name": "echo", "description": "", "inputSchema": LEGACY_TOOL_SCHEMA}]


@pytest.mark.asyncio
async def test_websocket_connection_runs_the_same_client_lifecycle(monkeypatch: Any) -> None:
    """A custom transport rides the v2 Client: the stored session is the negotiated one."""
    monkeypatch.setattr(
        "gobby.mcp_proxy.transports.websocket.websocket_client",
        lambda url, headers: legacy_transport(),
    )
    connection = WebSocketTransportConnection(
        MCPServerConfig(
            name="ws-legacy", project_id="p", transport="websocket", url="ws://legacy.test/mcp"
        )
    )

    session = await connection.connect()
    try:
        assert connection.state == ConnectionState.CONNECTED
        assert isinstance(session, ClientSession)
        assert session.protocol_version == LEGACY_PROTOCOL_VERSION
        await _assert_session_usable(session)
        assert await connection.health_check() is True
    finally:
        await connection.disconnect()

    state_after_disconnect: ConnectionState = connection.state
    assert state_after_disconnect == ConnectionState.DISCONNECTED
    assert connection.session is None


_STDIO_SERVER = """
from mcp.server.mcpserver import MCPServer

server = MCPServer("stdio-echo", version="1.2.3")


@server.tool()
def echo(text: str) -> str:
    return text


server.run("stdio")
"""


@pytest.mark.asyncio
async def test_stdio_connection_round_trips_a_real_subprocess(tmp_path: Any) -> None:
    """Spawn a real MCP 2.0 stdio server and drive it through StdioTransportConnection."""
    connection = StdioTransportConnection(
        MCPServerConfig(
            name="stdio-echo",
            project_id="p",
            transport="stdio",
            command=sys.executable,
            args=["-c", _STDIO_SERVER],
        ),
        stdio_errlog_path=str(tmp_path / "stdio-echo.log"),
    )

    session = await connection.connect()
    try:
        assert connection.state == ConnectionState.CONNECTED
        assert session.protocol_version in MODERN_PROTOCOL_VERSIONS
        assert session.server_info is not None and session.server_info.version == "1.2.3"
        await _assert_session_usable(session)
    finally:
        await connection.disconnect()

    state_after_disconnect: ConnectionState = connection.state
    assert state_after_disconnect == ConnectionState.DISCONNECTED
    assert connection._stdio_errlog_handle is None


_PID_STDIO_SERVER = """
import os

from mcp.server.mcpserver import MCPServer

server = MCPServer("stdio-pid", version="1.2.3")


@server.tool()
def pid() -> int:
    return os.getpid()


server.run("stdio")
"""


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        # A reaped zombie also counts as gone; waitpid clears it if it is ours.
        finished, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return finished == 0


@pytest.mark.asyncio
async def test_stdio_subprocess_exits_when_disconnected_from_another_task(tmp_path: Any) -> None:
    """connect_all() connects in a per-server task; disconnect_all() exits elsewhere."""
    connection = StdioTransportConnection(
        MCPServerConfig(
            name="stdio-pid",
            project_id="p",
            transport="stdio",
            command=sys.executable,
            args=["-c", _PID_STDIO_SERVER],
        ),
        stdio_errlog_path=str(tmp_path / "stdio-pid.log"),
    )

    session = await asyncio.create_task(connection.connect())
    owner_task = connection._owner_task
    assert owner_task is not None
    result = await session.call_tool("pid", {})
    assert result.structured_content is not None
    child_pid = int(result.structured_content["result"])
    assert _process_alive(child_pid)

    await asyncio.create_task(connection.disconnect())

    assert owner_task.done() and not owner_task.cancelled()
    assert connection.state == ConnectionState.DISCONNECTED
    assert connection.session is None
    assert connection._client_context is None
    assert connection._stdio_errlog_handle is None
    assert not _process_alive(child_pid), f"stdio server {child_pid} outlived disconnect()"
