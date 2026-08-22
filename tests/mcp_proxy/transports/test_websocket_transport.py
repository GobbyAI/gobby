"""Tests for WebSocket transport connection.

Exercises the real WebSocketTransportConnection code paths, including the
owner task that enters and exits the SDK ``Client``. ``Client`` is replaced by
``FakeClient`` for lifecycle tests; the raw ``websocket_client`` frame codec is
exercised against a fake socket. A real cross-task lifecycle over the wire
lives in ``test_negotiation.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCRequest, JSONRPCResponse

from gobby.mcp_proxy.models import ConnectionState, MCPError, MCPServerConfig
from gobby.mcp_proxy.transports.websocket import WebSocketTransportConnection, websocket_client
from tests._timing import wait_for_async_condition
from tests.mcp_proxy.transports._support import FakeClient, recording_transport

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


class _ClientHarness:
    """Patch ``websocket_client`` and ``Client`` together, recording the lifecycle."""

    def __init__(self, **client_kwargs: Any) -> None:
        self.lifecycle: list[str] = []
        self.transport_calls: list[tuple[str, dict[str, str] | None]] = []
        self.clients: list[FakeClient] = []
        self.client_kwargs = client_kwargs
        self.transport_enter_error: BaseException | None = None

    def fake_websocket_client(self, url: str, headers: dict[str, str] | None) -> Any:
        self.transport_calls.append((url, headers))
        return recording_transport(self.lifecycle, enter_error=self.transport_enter_error)

    def fake_client(self, transport: Any) -> FakeClient:
        client = FakeClient(transport, lifecycle=self.lifecycle, **self.client_kwargs)
        self.clients.append(client)
        return client

    def patches(self) -> Any:
        return (
            patch(
                "gobby.mcp_proxy.transports.websocket.websocket_client",
                side_effect=self.fake_websocket_client,
            ),
            patch("gobby.mcp_proxy.transports.base.Client", side_effect=self.fake_client),
        )


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
        assert conn.session is None
        assert conn._client_context is None
        assert conn._owner_task is None
        assert not conn.is_connected

    def test_config_stored(
        self, conn: WebSocketTransportConnection, config: MCPServerConfig
    ) -> None:
        assert conn.config is config


class TestWebSocketConnectAlreadyConnected:
    async def test_returns_existing_session(self, conn: WebSocketTransportConnection) -> None:
        session = AsyncMock()
        conn._state = ConnectionState.CONNECTED
        conn._session = session

        assert await conn.connect() is session


# ===========================================================================
# Connect success
# ===========================================================================


class TestWebSocketConnectSuccess:
    async def test_full_connect(self, conn: WebSocketTransportConnection) -> None:
        harness = _ClientHarness()
        ws_patch, client_patch = harness.patches()
        with ws_patch, client_patch:
            session = await conn.connect()

            assert conn.state == ConnectionState.CONNECTED
            assert conn.is_connected
            assert session is harness.clients[0].session
            client_context: object = conn._client_context
            assert client_context is harness.clients[0]
            assert conn._owner_task is not None and not conn._owner_task.done()
            assert harness.lifecycle == ["streams-open", "transport-enter", "handshake"]
            await conn.disconnect()

    async def test_connect_passes_url_and_headers(self) -> None:
        conn = WebSocketTransportConnection(
            _make_config(url="ws://example.test/mcp", headers={"Authorization": "Bearer t"})
        )
        harness = _ClientHarness()
        ws_patch, client_patch = harness.patches()
        with ws_patch, client_patch:
            await conn.connect()
            await conn.disconnect()

        assert harness.transport_calls == [("ws://example.test/mcp", {"Authorization": "Bearer t"})]

    async def test_client_wraps_the_websocket_transport(
        self, conn: WebSocketTransportConnection
    ) -> None:
        harness = _ClientHarness()
        ws_patch, client_patch = harness.patches()
        with ws_patch, client_patch:
            await conn.connect()
            # The Client owns the transport context; the connection never enters it directly.
            assert harness.clients[0].streams is not None
            assert conn._consecutive_failures == 0
            await conn.disconnect()


# ===========================================================================
# Connect failures
# ===========================================================================


class TestWebSocketConnectMissingURL:
    async def test_missing_url_raises_mcp_error(self) -> None:
        conn = WebSocketTransportConnection(_make_config(url=None))

        with pytest.raises(MCPError, match="URL is required"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED
        assert conn._client_context is None
        assert conn._owner_task is None


class TestWebSocketConnectTransportFailure:
    async def test_transport_enter_failure(self, conn: WebSocketTransportConnection) -> None:
        harness = _ClientHarness()
        harness.transport_enter_error = OSError("refused")
        ws_patch, client_patch = harness.patches()
        with ws_patch, client_patch, pytest.raises(MCPError, match="refused"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED
        assert conn._client_context is None
        assert conn._owner_task is None
        assert harness.clients[0].exited is False
        assert harness.lifecycle == []


class TestWebSocketConnectHandshakeFailure:
    async def test_handshake_failure_unwinds_transport_via_client(
        self, conn: WebSocketTransportConnection
    ) -> None:
        harness = _ClientHarness(handshake_error=RuntimeError("handshake boom"))
        ws_patch, client_patch = harness.patches()
        with ws_patch, client_patch, pytest.raises(MCPError, match="handshake boom"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED
        assert conn._client_context is None
        assert harness.lifecycle == [
            "streams-open",
            "transport-enter",
            "streams-closed",
            "transport-exit",
        ]
        assert harness.clients[0].exited is False


class TestWebSocketConnectCancellation:
    async def test_caller_cancellation_unwinds_owner_task(
        self, conn: WebSocketTransportConnection
    ) -> None:
        """Cancelling the connecting task must not leave the socket task group alive."""
        harness = _ClientHarness(handshake_gate=asyncio.Event())
        ws_patch, client_patch = harness.patches()
        with ws_patch, client_patch:
            connecting = asyncio.create_task(conn.connect())
            await wait_for_async_condition(
                lambda: "transport-enter" in harness.lifecycle,
                description="handshake to start",
            )
            owner_task = conn._owner_task
            assert owner_task is not None

            connecting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await connecting

        assert owner_task.done()
        assert conn._owner_task is None
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn._client_context is None
        assert harness.lifecycle == [
            "streams-open",
            "transport-enter",
            "streams-closed",
            "transport-exit",
        ]


class TestWebSocketConnectMCPErrorPassthrough:
    async def test_mcp_error_not_double_wrapped(self, conn: WebSocketTransportConnection) -> None:
        harness = _ClientHarness(handshake_error=MCPError("already wrapped"))
        ws_patch, client_patch = harness.patches()
        with ws_patch, client_patch, pytest.raises(MCPError) as excinfo:
            await conn.connect()

        assert str(excinfo.value) == "already wrapped"


class TestWebSocketConnectEmptyErrorMessage:
    async def test_empty_error_uses_type_name(self, conn: WebSocketTransportConnection) -> None:
        class SilentError(Exception):
            def __str__(self) -> str:
                return ""

        harness = _ClientHarness(handshake_error=SilentError())
        ws_patch, client_patch = harness.patches()
        with ws_patch, client_patch, pytest.raises(MCPError, match="SilentError"):
            await conn.connect()


# ===========================================================================
# Disconnect
# ===========================================================================


class TestWebSocketDisconnect:
    async def test_disconnect_clean_state(self, conn: WebSocketTransportConnection) -> None:
        await conn.disconnect()

        assert conn.state == ConnectionState.DISCONNECTED
        assert conn._client_context is None
        assert conn._owner_task is None

    async def test_disconnect_from_another_task_exits_client_in_owner_task(
        self, conn: WebSocketTransportConnection
    ) -> None:
        """connect_all() connects in one task and disconnect_all() in another."""
        harness = _ClientHarness()
        ws_patch, client_patch = harness.patches()
        with ws_patch, client_patch:
            await asyncio.create_task(conn.connect())
            owner_task = conn._owner_task
            assert owner_task is not None
            client = harness.clients[0]

            await asyncio.create_task(conn.disconnect())

        assert client.exited
        assert owner_task.done() and not owner_task.cancelled()
        assert harness.lifecycle == [
            "streams-open",
            "transport-enter",
            "handshake",
            "streams-closed",
            "transport-exit",
        ]
        assert conn._client_context is None
        assert conn._owner_task is None
        assert conn.session is None
        assert conn.state == ConnectionState.DISCONNECTED

    async def test_exit_error_is_logged_and_state_reset(
        self, conn: WebSocketTransportConnection, caplog: pytest.LogCaptureFixture
    ) -> None:
        harness = _ClientHarness(exit_error=RuntimeError("exit boom"))
        ws_patch, client_patch = harness.patches()
        with ws_patch, client_patch, caplog.at_level("WARNING", logger="gobby.mcp.client"):
            await conn.connect()
            await conn.disconnect()

        assert [r for r in caplog.records if "Error closing WebSocket client" in r.message]
        assert not [r for r in caplog.records if "Failed to connect" in r.message]
        assert conn._client_context is None
        assert conn._owner_task is None
        assert conn.state == ConnectionState.DISCONNECTED

    async def test_stalled_exit_is_cancelled(
        self, conn: WebSocketTransportConnection, caplog: pytest.LogCaptureFixture
    ) -> None:
        harness = _ClientHarness(exit_delay=60.0)
        ws_patch, client_patch = harness.patches()
        with (
            ws_patch,
            client_patch,
            patch.object(WebSocketTransportConnection, "_OWNER_TASK_SHUTDOWN_TIMEOUT", 0.01),
            caplog.at_level("DEBUG", logger="gobby.mcp.client"),
        ):
            await conn.connect()
            owner_task = conn._owner_task
            assert owner_task is not None
            await conn.disconnect()

        assert owner_task.cancelled()
        assert [r for r in caplog.records if "Owner task cancelled" in r.message]
        assert conn._client_context is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# Full lifecycle
# ===========================================================================


class TestWebSocketFullLifecycle:
    async def test_connect_then_disconnect(self, conn: WebSocketTransportConnection) -> None:
        harness = _ClientHarness()
        ws_patch, client_patch = harness.patches()
        with ws_patch, client_patch:
            await conn.connect()
            assert conn.is_connected
            await conn.disconnect()

        assert conn.state == ConnectionState.DISCONNECTED
        assert conn.session is None
        assert harness.lifecycle == [
            "streams-open",
            "transport-enter",
            "handshake",
            "streams-closed",
            "transport-exit",
        ]

    async def test_reconnect_after_disconnect(self, conn: WebSocketTransportConnection) -> None:
        harness = _ClientHarness()
        ws_patch, client_patch = harness.patches()
        with ws_patch, client_patch:
            await conn.connect()
            await conn.disconnect()
            await conn.connect()

            assert conn.is_connected
            assert len(harness.clients) == 2
            client_context: object = conn._client_context
            assert client_context is harness.clients[1]
            await conn.disconnect()


# ===========================================================================
# Full lifecycle & base properties
# ===========================================================================


class TestWebSocketBaseProperties:
    def test_is_connected_requires_state_and_session(
        self, conn: WebSocketTransportConnection
    ) -> None:
        assert not conn.is_connected
        conn._state = ConnectionState.CONNECTED
        assert not conn.is_connected
        conn._session = AsyncMock()
        assert conn.is_connected

    async def test_health_check_not_connected(self, conn: WebSocketTransportConnection) -> None:
        assert await conn.health_check() is False

    async def test_health_check_connected_success(self, conn: WebSocketTransportConnection) -> None:
        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
        conn._state = ConnectionState.CONNECTED
        conn._session = session

        assert await conn.health_check() is True
        assert conn.last_health_error is None

    async def test_health_check_connected_failure(self, conn: WebSocketTransportConnection) -> None:
        session = AsyncMock()
        session.list_tools = AsyncMock(side_effect=RuntimeError("down"))
        conn._state = ConnectionState.CONNECTED
        conn._session = session

        assert await conn.health_check() is False
        assert conn.last_health_error == "RuntimeError: down"


# ===========================================================================
# websocket_client frame codec
# ===========================================================================


class _FakeSocket:
    """Async iterator of inbound frames that records outbound sends."""

    def __init__(self, inbound: list[str]) -> None:
        self._inbound = inbound
        self.sent: list[str] = []
        self.closed = asyncio.Event()
        self.sent_any = asyncio.Event()

    def __aiter__(self) -> _FakeSocket:
        return self

    async def __anext__(self) -> str:
        if self._inbound:
            return self._inbound.pop(0)
        await self.closed.wait()
        raise StopAsyncIteration

    async def send(self, payload: str) -> None:
        self.sent.append(payload)
        self.sent_any.set()


def _patched_ws_connect(socket: _FakeSocket) -> Any:
    context = AsyncMock()
    context.__aenter__.return_value = socket
    context.__aexit__.return_value = False
    return patch("gobby.mcp_proxy.transports.websocket.ws_connect", return_value=context)


class TestWebSocketClientCodec:
    async def test_forwards_additional_headers_and_subprotocol(self) -> None:
        socket = _FakeSocket([])
        headers = {"Authorization": "Bearer secret"}
        with _patched_ws_connect(socket) as mock_ws_connect:
            async with websocket_client("ws://localhost:9090/ws", headers):
                socket.closed.set()
            assert mock_ws_connect.call_args.kwargs["additional_headers"] == headers
            assert mock_ws_connect.call_args.kwargs["subprotocols"] == ["mcp"]

    async def test_inbound_frames_validate_to_jsonrpc_messages(self) -> None:
        socket = _FakeSocket(
            [json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"tools": []}}), "{not json"]
        )
        with _patched_ws_connect(socket):
            async with websocket_client("ws://localhost:9090/ws", None) as (read, _write):
                first = await read.receive()
                second = await read.receive()
                socket.closed.set()

        assert isinstance(first, SessionMessage)
        assert isinstance(first.message, JSONRPCResponse)
        assert first.message.id == 7
        # Malformed frames surface as exceptions on the read stream, not crashes.
        assert isinstance(second, Exception)

    async def test_outbound_messages_serialize_camel_case_without_nulls(self) -> None:
        socket = _FakeSocket([])
        with _patched_ws_connect(socket):
            async with websocket_client("ws://localhost:9090/ws", None) as (_read, write):
                await write.send(
                    SessionMessage(
                        JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/list", params=None)
                    )
                )
                await asyncio.wait_for(socket.sent_any.wait(), timeout=5)
                socket.closed.set()

        assert socket.sent == ['{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}']
