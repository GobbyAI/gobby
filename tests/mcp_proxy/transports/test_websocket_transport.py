"""Tests for WebSocket transport connection.

Exercises the real WebSocketTransportConnection code paths. Only the MCP SDK's
websocket_client and ClientSession are mocked (external I/O).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.models import ConnectionState, MCPError, MCPServerConfig
from gobby.mcp_proxy.transports.websocket import WebSocketTransportConnection

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> MCPServerConfig:
    """Create a real MCPServerConfig for WebSocket transport."""
    defaults = {
        "name": "test-ws",
        "project_id": "proj-003",
        "transport": "websocket",
        "url": "ws://localhost:9090/ws",
    }
    defaults.update(overrides)
    return MCPServerConfig(**defaults)


def _mock_session() -> AsyncMock:
    """Create a mock ClientSession with initialize()."""
    session = AsyncMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=[])
    return session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> MCPServerConfig:
    return _make_config()


@pytest.fixture
def conn(config: MCPServerConfig) -> WebSocketTransportConnection:
    return WebSocketTransportConnection(config)


# ===========================================================================
# Construction & initial state
# ===========================================================================


class TestWebSocketInit:
    def test_initial_state(self, conn: WebSocketTransportConnection) -> None:
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn.is_connected is False
        assert conn.session is None
        assert conn._session_context is None
        assert conn._transport_context is None

    def test_config_stored(self, conn: WebSocketTransportConnection) -> None:
        assert conn.config.name == "test-ws"
        assert conn.config.url == "ws://localhost:9090/ws"

    def test_auth_token_default_none(self, conn: WebSocketTransportConnection) -> None:
        assert conn._auth_token is None

    def test_auth_token_and_callback(self, config: MCPServerConfig) -> None:
        async def refresh() -> str:
            return "refreshed"

        c = WebSocketTransportConnection(config, auth_token="tok", token_refresh_callback=refresh)
        assert c._auth_token == "tok"
        assert c._token_refresh_callback is refresh


# ===========================================================================
# connect() — already connected
# ===========================================================================


class TestWebSocketConnectAlreadyConnected:
    async def test_returns_existing_session(self, conn: WebSocketTransportConnection) -> None:
        fake_session = MagicMock()
        conn._state = ConnectionState.CONNECTED
        conn._session = fake_session

        result = await conn.connect()
        assert result is fake_session
        assert conn.state == ConnectionState.CONNECTED


# ===========================================================================
# connect() — successful
# ===========================================================================


class TestWebSocketConnectSuccess:
    @patch("gobby.mcp_proxy.transports.websocket.ClientSession")
    @patch("gobby.mcp_proxy.transports.websocket.websocket_client")
    async def test_full_connect(
        self,
        mock_ws_client: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: WebSocketTransportConnection,
    ) -> None:
        mock_read = MagicMock()
        mock_write = MagicMock()

        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(return_value=(mock_read, mock_write))
        mock_ws_client.return_value = mock_transport_ctx

        mock_session = _mock_session()
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_client_session_cls.return_value = mock_session_ctx

        result = await conn.connect()

        assert result is mock_session
        assert conn.state == ConnectionState.CONNECTED
        assert conn.is_connected is True
        assert conn._consecutive_failures == 0
        assert conn._session is mock_session
        assert conn._session_context is mock_session_ctx
        assert conn._transport_context is mock_transport_ctx

    @patch("gobby.mcp_proxy.transports.websocket.ClientSession")
    @patch("gobby.mcp_proxy.transports.websocket.websocket_client")
    async def test_connect_passes_url(
        self,
        mock_ws_client: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: WebSocketTransportConnection,
    ) -> None:
        """Verify url from config is passed to websocket_client."""
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_ws_client.return_value = mock_transport_ctx

        mock_session = _mock_session()
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_client_session_cls.return_value = mock_session_ctx

        await conn.connect()

        mock_ws_client.assert_called_once_with("ws://localhost:9090/ws")


# ===========================================================================
# connect() — missing URL
# ===========================================================================


class TestWebSocketConnectMissingURL:
    async def test_missing_url_raises_mcp_error(self) -> None:
        cfg = _make_config()
        cfg.url = None
        c = WebSocketTransportConnection(cfg)

        with pytest.raises(MCPError, match="WebSocket connection failed"):
            await c.connect()

        assert c.state == ConnectionState.FAILED
        assert c._session is None
        assert c._session_context is None
        assert c._transport_context is None


# ===========================================================================
# connect() — transport entry failure
# ===========================================================================


class TestWebSocketConnectTransportFailure:
    @patch("gobby.mcp_proxy.transports.websocket.websocket_client")
    async def test_transport_aenter_failure(
        self,
        mock_ws_client: MagicMock,
        conn: WebSocketTransportConnection,
    ) -> None:
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(side_effect=ConnectionError("ws refused"))
        mock_ws_client.return_value = mock_transport_ctx

        with pytest.raises(MCPError, match="WebSocket connection failed.*ws refused"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED
        assert conn._session is None
        assert conn._transport_context is None


# ===========================================================================
# connect() — session entry failure
# ===========================================================================


class TestWebSocketConnectSessionFailure:
    @patch("gobby.mcp_proxy.transports.websocket.ClientSession")
    @patch("gobby.mcp_proxy.transports.websocket.websocket_client")
    async def test_session_aenter_failure_cleans_transport(
        self,
        mock_ws_client: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: WebSocketTransportConnection,
    ) -> None:
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_transport_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ws_client.return_value = mock_transport_ctx

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("session init failed"))
        mock_client_session_cls.return_value = mock_session_ctx

        with pytest.raises(MCPError, match="WebSocket connection failed.*session init failed"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED
        mock_transport_ctx.__aexit__.assert_awaited_once()


# ===========================================================================
# connect() — initialize() failure
# ===========================================================================


class TestWebSocketConnectInitializeFailure:
    @patch("gobby.mcp_proxy.transports.websocket.ClientSession")
    @patch("gobby.mcp_proxy.transports.websocket.websocket_client")
    async def test_initialize_failure_cleans_both(
        self,
        mock_ws_client: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: WebSocketTransportConnection,
    ) -> None:
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_transport_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ws_client.return_value = mock_transport_ctx

        mock_session = _mock_session()
        mock_session.initialize = AsyncMock(side_effect=ConnectionError("handshake failed"))

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client_session_cls.return_value = mock_session_ctx

        with pytest.raises(MCPError, match="WebSocket connection failed.*handshake failed"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED
        mock_session_ctx.__aexit__.assert_awaited_once()
        mock_transport_ctx.__aexit__.assert_awaited_once()


# ===========================================================================
# connect() — cleanup errors during failure are suppressed
# ===========================================================================


class TestWebSocketConnectCleanupErrors:
    @patch("gobby.mcp_proxy.transports.websocket.ClientSession")
    @patch("gobby.mcp_proxy.transports.websocket.websocket_client")
    async def test_session_cleanup_error_suppressed(
        self,
        mock_ws_client: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: WebSocketTransportConnection,
    ) -> None:
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_transport_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ws_client.return_value = mock_transport_ctx

        mock_session = _mock_session()
        mock_session.initialize = AsyncMock(side_effect=ValueError("init fail"))

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(side_effect=RuntimeError("cleanup boom"))
        mock_client_session_cls.return_value = mock_session_ctx

        with pytest.raises(MCPError, match="init fail"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED

    @patch("gobby.mcp_proxy.transports.websocket.ClientSession")
    @patch("gobby.mcp_proxy.transports.websocket.websocket_client")
    async def test_transport_cleanup_error_suppressed(
        self,
        mock_ws_client: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: WebSocketTransportConnection,
    ) -> None:
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_transport_ctx.__aexit__ = AsyncMock(side_effect=OSError("transport cleanup fail"))
        mock_ws_client.return_value = mock_transport_ctx

        mock_session = _mock_session()
        mock_session.initialize = AsyncMock(side_effect=ValueError("init fail"))

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client_session_cls.return_value = mock_session_ctx

        with pytest.raises(MCPError, match="init fail"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED


# ===========================================================================
# connect() — MCPError not double-wrapped
# ===========================================================================


class TestWebSocketConnectMCPErrorPassthrough:
    @patch("gobby.mcp_proxy.transports.websocket.websocket_client")
    async def test_mcp_error_re_raised_directly(
        self,
        mock_ws_client: MagicMock,
        conn: WebSocketTransportConnection,
    ) -> None:
        original = MCPError("original ws error")
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(side_effect=original)
        mock_ws_client.return_value = mock_transport_ctx

        with pytest.raises(MCPError, match="original ws error") as exc_info:
            await conn.connect()

        assert exc_info.value is original


# ===========================================================================
# connect() — empty error message
# ===========================================================================


class TestWebSocketConnectEmptyErrorMessage:
    @patch("gobby.mcp_proxy.transports.websocket.websocket_client")
    async def test_empty_str_exception_uses_type_name(
        self,
        mock_ws_client: MagicMock,
        conn: WebSocketTransportConnection,
    ) -> None:
        class SilentError(Exception):
            def __str__(self) -> str:
                return ""

        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(side_effect=SilentError())
        mock_ws_client.return_value = mock_transport_ctx

        with pytest.raises(MCPError, match="SilentError.*Connection closed or timed out"):
            await conn.connect()


# ===========================================================================
# disconnect() — no contexts
# ===========================================================================


class TestWebSocketDisconnectNoContexts:
    async def test_disconnect_clean_state(self, conn: WebSocketTransportConnection) -> None:
        await conn.disconnect()
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn._session_context is None
        assert conn._transport_context is None
        assert conn._session is None


# ===========================================================================
# disconnect() — happy path with both contexts
# ===========================================================================


class TestWebSocketDisconnectBothContexts:
    async def test_cleans_both_contexts(self, conn: WebSocketTransportConnection) -> None:
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        conn._session_context = mock_session_ctx
        conn._session = MagicMock()

        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aexit__ = AsyncMock(return_value=False)
        conn._transport_context = mock_transport_ctx

        await conn.disconnect()

        assert conn._session_context is None
        assert conn._session is None
        assert conn._transport_context is None
        assert conn.state == ConnectionState.DISCONNECTED

        mock_session_ctx.__aexit__.assert_awaited_once()
        mock_transport_ctx.__aexit__.assert_awaited_once()


# ===========================================================================
# disconnect() — session timeout
# ===========================================================================


class TestWebSocketDisconnectSessionTimeout:
    async def test_session_timeout_handled(self, conn: WebSocketTransportConnection) -> None:
        async def slow_exit(*args: Any) -> None:
            raise TimeoutError()

        mock_session_ctx = MagicMock()
        mock_session_ctx.__aexit__ = slow_exit
        conn._session_context = mock_session_ctx
        conn._session = MagicMock()

        await conn.disconnect()
        assert conn._session_context is None
        assert conn._session is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# disconnect() — session RuntimeError (cancel scope)
# ===========================================================================


class TestWebSocketDisconnectSessionRuntimeError:
    async def test_cancel_scope_error_suppressed(self, conn: WebSocketTransportConnection) -> None:
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aexit__ = AsyncMock(side_effect=RuntimeError("cannot exit cancel scope"))
        conn._session_context = mock_session_ctx
        conn._session = MagicMock()

        await conn.disconnect()
        assert conn._session_context is None
        assert conn.state == ConnectionState.DISCONNECTED

    async def test_other_runtime_error_handled(self, conn: WebSocketTransportConnection) -> None:
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aexit__ = AsyncMock(side_effect=RuntimeError("something else entirely"))
        conn._session_context = mock_session_ctx
        conn._session = MagicMock()

        await conn.disconnect()
        assert conn._session_context is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# disconnect() — session generic Exception
# ===========================================================================


class TestWebSocketDisconnectSessionGenericError:
    async def test_generic_exception_handled(self, conn: WebSocketTransportConnection) -> None:
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aexit__ = AsyncMock(side_effect=ValueError("weird"))
        conn._session_context = mock_session_ctx
        conn._session = MagicMock()

        await conn.disconnect()
        assert conn._session_context is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# disconnect() — transport timeout
# ===========================================================================


class TestWebSocketDisconnectTransportTimeout:
    async def test_transport_timeout_handled(self, conn: WebSocketTransportConnection) -> None:
        async def slow_exit(*args: Any) -> None:
            raise TimeoutError()

        mock_transport_ctx = MagicMock()
        mock_transport_ctx.__aexit__ = slow_exit
        conn._transport_context = mock_transport_ctx

        await conn.disconnect()
        assert conn._transport_context is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# disconnect() — transport RuntimeError (cancel scope)
# ===========================================================================


class TestWebSocketDisconnectTransportRuntimeError:
    async def test_cancel_scope_error_suppressed(self, conn: WebSocketTransportConnection) -> None:
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aexit__ = AsyncMock(side_effect=RuntimeError("cancel scope blah"))
        conn._transport_context = mock_transport_ctx

        await conn.disconnect()
        assert conn._transport_context is None
        assert conn.state == ConnectionState.DISCONNECTED

    async def test_other_runtime_error_logged(self, conn: WebSocketTransportConnection) -> None:
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aexit__ = AsyncMock(
            side_effect=RuntimeError("unexpected transport error")
        )
        conn._transport_context = mock_transport_ctx

        await conn.disconnect()
        assert conn._transport_context is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# disconnect() — transport generic Exception
# ===========================================================================


class TestWebSocketDisconnectTransportGenericError:
    async def test_generic_exception_handled(self, conn: WebSocketTransportConnection) -> None:
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aexit__ = AsyncMock(side_effect=OSError("broken pipe"))
        conn._transport_context = mock_transport_ctx

        await conn.disconnect()
        assert conn._transport_context is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# Full connect -> disconnect cycle
# ===========================================================================


class TestWebSocketFullLifecycle:
    @patch("gobby.mcp_proxy.transports.websocket.ClientSession")
    @patch("gobby.mcp_proxy.transports.websocket.websocket_client")
    async def test_connect_then_disconnect(
        self,
        mock_ws_client: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: WebSocketTransportConnection,
    ) -> None:
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_transport_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ws_client.return_value = mock_transport_ctx

        mock_session = _mock_session()
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client_session_cls.return_value = mock_session_ctx

        # Connect
        result = await conn.connect()
        assert result is mock_session
        assert conn.state == ConnectionState.CONNECTED
        assert conn.is_connected is True

        # Disconnect
        await conn.disconnect()
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn.is_connected is False
        assert conn._session is None
        assert conn._session_context is None
        assert conn._transport_context is None


# ===========================================================================
# Base class properties exercised through WebSocketTransportConnection
# ===========================================================================


class TestWebSocketBaseProperties:
    def test_is_connected_requires_both_state_and_session(
        self, conn: WebSocketTransportConnection
    ) -> None:
        conn._state = ConnectionState.CONNECTED
        conn._session = None
        assert conn.is_connected is False

        conn._state = ConnectionState.DISCONNECTED
        conn._session = MagicMock()
        assert conn.is_connected is False

        conn._state = ConnectionState.CONNECTED
        assert conn.is_connected is True

    def test_set_auth_token(self, conn: WebSocketTransportConnection) -> None:
        conn.set_auth_token("new-token")
        assert conn._auth_token == "new-token"

    async def test_health_check_not_connected(self, conn: WebSocketTransportConnection) -> None:
        result = await conn.health_check()
        assert result is False

    async def test_health_check_connected_success(self, conn: WebSocketTransportConnection) -> None:
        mock_session = AsyncMock()
        mock_session.list_tools = AsyncMock(return_value=[])
        conn._state = ConnectionState.CONNECTED
        conn._session = mock_session

        result = await conn.health_check()
        assert result is True
        assert conn._consecutive_failures == 0
        assert conn._last_health_check is not None

    async def test_health_check_connected_failure(self, conn: WebSocketTransportConnection) -> None:
        mock_session = AsyncMock()
        mock_session.list_tools = AsyncMock(side_effect=Exception("boom"))
        conn._state = ConnectionState.CONNECTED
        conn._session = mock_session

        result = await conn.health_check()
        assert result is False
        assert conn._consecutive_failures == 1
