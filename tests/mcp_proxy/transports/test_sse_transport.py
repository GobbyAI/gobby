"""Tests for the SDK-backed SSE transport connection."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.mcp import MCPConfigManager
from gobby.mcp_proxy.models import ConnectionState, MCPServerConfig
from gobby.mcp_proxy.transports.factory import create_transport_connection
from gobby.mcp_proxy.transports.sse import SSETransportConnection

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
    read_stream = MagicMock()
    write_stream = MagicMock()

    @asynccontextmanager
    async def fake_sse_client(
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 5,
    ) -> AsyncIterator[tuple[MagicMock, MagicMock]]:
        captured.update(url=url, headers=headers, timeout=timeout)
        lifecycle.append("transport-enter")
        try:
            yield read_stream, write_stream
        finally:
            lifecycle.append("transport-exit")

    session = AsyncMock()
    session.initialize = AsyncMock()
    session_context = AsyncMock()
    session_context.__aenter__.return_value = session
    session_context.__aexit__.return_value = False

    with (
        patch("gobby.mcp_proxy.transports.sse.sse_client", side_effect=fake_sse_client),
        patch(
            "gobby.mcp_proxy.transports.http.ClientSession",
            return_value=session_context,
        ) as client_session,
    ):
        result = await connection.connect()
        assert result is session
        assert connection.state == ConnectionState.CONNECTED
        client_session.assert_called_once_with(read_stream, write_stream)
        session.initialize.assert_awaited_once()

        await connection.disconnect()

    assert connection.state == ConnectionState.DISCONNECTED
    assert lifecycle == ["transport-enter", "transport-exit"]
    assert captured == {
        "url": "https://example.test/sse",
        "headers": {"X-Tenant": "example"},
        "timeout": 30.0,
    }
    session_context.__aexit__.assert_awaited_once_with(None, None, None)
