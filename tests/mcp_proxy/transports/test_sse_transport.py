"""Tests for the SDK-backed SSE transport connection."""

from typing import Any
from unittest.mock import patch

import pytest

from gobby.config.mcp import MCPConfigManager
from gobby.mcp_proxy.models import ConnectionState, MCPServerConfig
from gobby.mcp_proxy.transports.factory import create_transport_connection
from gobby.mcp_proxy.transports.sse import SSETransportConnection
from tests.mcp_proxy.transports._support import FakeClient, recording_transport

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_persisted_sse_config_connects_after_reload(tmp_path) -> None:
    """A saved SSE config remains connectable after recreating the config manager."""
    config_path = tmp_path / "mcp-servers.json"
    original = MCPServerConfig(
        name="events",
        project_id="global",
        transport="sse",
        url="https://example.test/sse",
        headers={"X-Tenant": "example"},
        connect_timeout=4.0,
    )
    MCPConfigManager(str(config_path)).save_servers([original])

    reloaded = MCPConfigManager(str(config_path)).load_servers()
    assert len(reloaded) == 1
    connection = create_transport_connection(reloaded[0])
    assert isinstance(connection, SSETransportConnection)

    lifecycle: list[str] = []
    captured: dict[str, Any] = {}
    clients: list[FakeClient] = []

    def fake_sse_client(url: str, headers: dict[str, str] | None = None, timeout: float = 5) -> Any:
        captured.update(url=url, headers=headers, timeout=timeout)
        return recording_transport(lifecycle)

    def fake_client(transport: Any) -> FakeClient:
        client = FakeClient(transport, lifecycle=lifecycle)
        clients.append(client)
        return client

    with (
        patch("gobby.mcp_proxy.transports.sse.sse_client", side_effect=fake_sse_client),
        patch("gobby.mcp_proxy.transports.http.Client", side_effect=fake_client),
    ):
        result = await connection.connect()
        assert result is clients[0].session
        assert connection.state == ConnectionState.CONNECTED
        assert lifecycle == ["streams-open", "transport-enter", "handshake"]

        await connection.disconnect()

    assert connection.state == ConnectionState.DISCONNECTED
    assert lifecycle == [
        "streams-open",
        "transport-enter",
        "handshake",
        "streams-closed",
        "transport-exit",
    ]
    # connect_timeout is not persisted, so the reloaded config carries the
    # default 30s; the SSE client's 300s read timeout is the SDK default.
    assert captured == {
        "url": "https://example.test/sse",
        "headers": {"X-Tenant": "example"},
        "timeout": 30.0,
    }
