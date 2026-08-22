"""Tests for HTTP transport connection.

Exercises the real HTTPTransportConnection code paths. The MCP SDK's
``streamable_http_client`` (network I/O) and ``Client`` are replaced by fakes;
the declared ``httpx2.AsyncClient`` is built for real and inspected.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2
import pytest

from gobby.mcp_proxy.models import ConnectionState, MCPError, MCPServerConfig
from gobby.mcp_proxy.transports.http import (
    MCP_HTTP_READ_TIMEOUT_SECONDS,
    MCP_HTTP_TIMEOUT_SECONDS,
    HTTPTransportConnection,
    build_mcp_http_client,
)
from tests._timing import drain_asyncio_tasks, wait_forever
from tests.mcp_proxy.transports._support import FakeClient, recording_transport

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> MCPServerConfig:
    """Create a real MCPServerConfig for HTTP transport."""
    defaults: dict[str, Any] = {
        "name": "test-http",
        "project_id": "proj-001",
        "transport": "http",
        "url": "http://localhost:8080/mcp",
        "headers": {"Authorization": "Bearer tok"},
        "connect_timeout": 2.0,
    }
    defaults.update(overrides)
    return MCPServerConfig(**defaults)


class _ClientHarness:
    """Patch ``streamable_http_client`` and ``Client`` together, recording the lifecycle."""

    def __init__(self, **client_kwargs: Any) -> None:
        self.lifecycle: list[str] = []
        self.transport_calls: list[tuple[str, Any]] = []
        self.clients: list[FakeClient] = []
        self.client_kwargs = client_kwargs
        self.transport_enter_error: BaseException | None = None

    def fake_streamable_http(self, url: str, *, http_client: Any = None) -> Any:
        self.transport_calls.append((url, http_client))
        return recording_transport(self.lifecycle, enter_error=self.transport_enter_error)

    def fake_client(self, transport: Any) -> FakeClient:
        client = FakeClient(transport, lifecycle=self.lifecycle, **self.client_kwargs)
        self.clients.append(client)
        return client

    def patches(self) -> Any:
        return (
            patch(
                "gobby.mcp_proxy.transports.http.streamable_http_client",
                side_effect=self.fake_streamable_http,
            ),
            patch("gobby.mcp_proxy.transports.base.Client", side_effect=self.fake_client),
        )

    @property
    def url(self) -> str:
        return self.transport_calls[0][0]

    @property
    def http_client(self) -> Any:
        return self.transport_calls[0][1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> MCPServerConfig:
    return _make_config()


@pytest.fixture
def conn(config: MCPServerConfig) -> HTTPTransportConnection:
    return HTTPTransportConnection(config)


# ===========================================================================
# Construction & initial state
# ===========================================================================


class TestHTTPInit:
    def test_initial_state(self, conn: HTTPTransportConnection) -> None:
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn.is_connected is False
        assert conn.session is None
        assert conn._owner_task is None
        assert conn._disconnect_event is None
        assert conn._session_ready is None
        assert conn._connection_error is None
        assert conn._client_context is None

    def test_config_stored(self, conn: HTTPTransportConnection) -> None:
        assert conn.config.name == "test-http"
        assert conn.config.url == "http://localhost:8080/mcp"


# ===========================================================================
# connect() — early return when already connected
# ===========================================================================


class TestHTTPConnectAlreadyConnected:
    @pytest.mark.asyncio
    async def test_returns_existing_session(self, conn: HTTPTransportConnection) -> None:
        fake_session = MagicMock()
        conn._state = ConnectionState.CONNECTED
        conn._session = fake_session

        result = await conn.connect()
        assert result is fake_session
        # State unchanged
        assert conn.state == ConnectionState.CONNECTED


# ===========================================================================
# connect() — successful connection via _run_connection
# ===========================================================================


class TestHTTPConnectSuccess:
    @pytest.mark.asyncio
    async def test_full_connect_lifecycle(self, conn: HTTPTransportConnection) -> None:
        """connect() goes CONNECTING -> CONNECTED with the Client owning the session."""
        harness = _ClientHarness()
        http_patch, client_patch = harness.patches()
        with http_patch, client_patch:
            result = await conn.connect()
            assert result is harness.clients[0].session
            assert conn.state == ConnectionState.CONNECTED
            assert conn.is_connected is True
            assert conn._consecutive_failures == 0
            client_context: object = conn._client_context
            assert client_context is harness.clients[0]
            assert harness.lifecycle == ["streams-open", "transport-enter", "handshake"]

            await conn.disconnect()

        assert harness.lifecycle[-2:] == ["streams-closed", "transport-exit"]

    @pytest.mark.asyncio
    async def test_connect_passes_url_and_declared_httpx2_client(
        self, conn: HTTPTransportConnection
    ) -> None:
        """The transport gets the URL plus a managed httpx2 client carrying the headers."""
        harness = _ClientHarness()
        http_patch, client_patch = harness.patches()
        with http_patch, client_patch:
            await conn.connect()
            assert harness.url == "http://localhost:8080/mcp"
            assert isinstance(harness.http_client, httpx2.AsyncClient)
            assert harness.http_client.headers["Authorization"] == "Bearer tok"
            assert not harness.http_client.is_closed
            await conn.disconnect()

        # The AsyncExitStack owning the client closes it after the transport.
        assert harness.http_client.is_closed


class TestBuildMcpHttpClient:
    @pytest.mark.asyncio
    async def test_applies_mcp_timeout_profile_and_redirects(self) -> None:
        client = build_mcp_http_client({"X-Tenant": "t"})
        try:
            assert client.follow_redirects is True
            assert client.headers["X-Tenant"] == "t"
            assert client.timeout == httpx2.Timeout(
                MCP_HTTP_TIMEOUT_SECONDS, read=MCP_HTTP_READ_TIMEOUT_SECONDS
            )
            assert (MCP_HTTP_TIMEOUT_SECONDS, MCP_HTTP_READ_TIMEOUT_SECONDS) == (30.0, 300.0)
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_accepts_no_headers(self) -> None:
        client = build_mcp_http_client(None)
        try:
            assert "Authorization" not in client.headers
        finally:
            await client.aclose()


# ===========================================================================
# connect() — reconnect path (existing _owner_task)
# ===========================================================================


class TestHTTPConnectReconnect:
    @pytest.mark.asyncio
    async def test_reconnect_cleans_old_task(self, conn: HTTPTransportConnection) -> None:
        """If _owner_task already exists, connect() calls disconnect() first."""
        harness = _ClientHarness()
        old_task = MagicMock()
        old_task.done.return_value = True
        conn._owner_task = old_task

        http_patch, client_patch = harness.patches()
        with http_patch, client_patch:
            result = await conn.connect()
            assert result is harness.clients[0].session
            assert conn.state == ConnectionState.CONNECTED
            await conn.disconnect()


# ===========================================================================
# connect() — timeout
# ===========================================================================


class TestHTTPConnectTimeout:
    @pytest.mark.asyncio
    async def test_timeout_transitions_to_failed(self, conn: HTTPTransportConnection) -> None:
        """If _session_ready never fires, connect raises MCPError after timeout."""
        conn.config = _make_config(connect_timeout=0.05)

        # Patch _run_connection to just sleep forever (never signals ready)
        async def slow_connection() -> None:
            await wait_forever()

        with patch.object(conn, "_run_connection", slow_connection):
            with pytest.raises(MCPError, match="Connection timeout"):
                await conn.connect()

        assert conn.state == ConnectionState.FAILED
        assert conn._owner_task is None

    @pytest.mark.asyncio
    async def test_timeout_includes_server_name(self) -> None:
        cfg = _make_config(name="my-server", connect_timeout=0.05)
        c = HTTPTransportConnection(cfg)

        async def slow() -> None:
            await wait_forever()

        with patch.object(c, "_run_connection", slow):
            with pytest.raises(MCPError, match="my-server"):
                await c.connect()


# ===========================================================================
# connect() — connection error propagation
# ===========================================================================


class TestHTTPConnectError:
    @pytest.mark.asyncio
    async def test_connection_error_propagated(self, conn: HTTPTransportConnection) -> None:
        """When _run_connection sets _connection_error, connect() re-raises it."""
        error = MCPError("HTTP connection failed: refused")

        async def fail_connection() -> None:
            assert conn._session_ready is not None
            conn._connection_error = error
            conn._session_ready.set()

        with patch.object(conn, "_run_connection", fail_connection):
            with pytest.raises(MCPError, match="refused"):
                await conn.connect()

        assert conn.state == ConnectionState.FAILED
        assert conn._owner_task is None

    @pytest.mark.asyncio
    async def test_connection_error_cleared_after_raise(
        self, conn: HTTPTransportConnection
    ) -> None:
        """_connection_error is set to None before raising."""
        error = MCPError("boom")

        async def fail() -> None:
            assert conn._session_ready is not None
            conn._connection_error = error
            conn._session_ready.set()

        with patch.object(conn, "_run_connection", fail):
            with pytest.raises(MCPError):
                await conn.connect()

        # The error reference is cleared
        assert conn._connection_error is None


# ===========================================================================
# _run_connection — error paths
# ===========================================================================


class TestHTTPRunConnection:
    @pytest.mark.asyncio
    async def test_missing_url_sets_connection_error(self) -> None:
        """When config.url is None, _run_connection records a ValueError-based MCPError."""
        cfg = _make_config(url=None)
        # url validation in MCPServerConfig won't catch None at construction because
        # we need to bypass validate() — set url to None after construction
        c = HTTPTransportConnection(cfg)
        c.config.url = None
        c._disconnect_event = asyncio.Event()
        c._session_ready = asyncio.Event()

        await c._run_connection()

        assert c._connection_error is not None
        assert "HTTP connection failed" in str(c._connection_error)
        assert c._session is None
        assert c.state == ConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_events_not_initialized_raises_runtime_error(
        self, conn: HTTPTransportConnection
    ) -> None:
        """If events not set, _run_connection raises RuntimeError."""
        conn._disconnect_event = None
        conn._session_ready = None

        with pytest.raises(RuntimeError, match="Connection events not initialized"):
            await conn._run_connection()

    @pytest.mark.asyncio
    async def test_transport_exception_wraps_as_mcp_error(
        self, conn: HTTPTransportConnection
    ) -> None:
        """Non-MCPError exceptions from the transport get wrapped in MCPError."""
        harness = _ClientHarness()
        harness.transport_enter_error = OSError("network down")
        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

        http_patch, client_patch = harness.patches()
        with http_patch, client_patch:
            await conn._run_connection()

        assert isinstance(conn._connection_error, MCPError)
        assert "network down" in str(conn._connection_error)
        assert conn._session is None
        assert conn.state == ConnectionState.DISCONNECTED
        assert harness.http_client.is_closed

    @pytest.mark.asyncio
    async def test_handshake_exception_wraps_as_mcp_error(
        self, conn: HTTPTransportConnection
    ) -> None:
        """A negotiation failure inside Client surfaces as the connection error."""
        harness = _ClientHarness(handshake_error=RuntimeError("handshake boom"))
        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

        http_patch, client_patch = harness.patches()
        with http_patch, client_patch:
            await conn._run_connection()

        assert isinstance(conn._connection_error, MCPError)
        assert "handshake boom" in str(conn._connection_error)
        assert harness.lifecycle == [
            "streams-open",
            "transport-enter",
            "streams-closed",
            "transport-exit",
        ]

    @pytest.mark.asyncio
    async def test_mcp_error_not_double_wrapped(self, conn: HTTPTransportConnection) -> None:
        """If exception is already MCPError, it's stored directly."""
        original = MCPError("original error")
        harness = _ClientHarness(handshake_error=original)
        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

        http_patch, client_patch = harness.patches()
        with http_patch, client_patch:
            await conn._run_connection()

        assert conn._connection_error is original

    @pytest.mark.asyncio
    async def test_empty_error_message_uses_type_name(self, conn: HTTPTransportConnection) -> None:
        """Exceptions with empty str() get a type-name-based message."""

        class SilentError(Exception):
            def __str__(self) -> str:
                return ""

        harness = _ClientHarness(handshake_error=SilentError())
        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

        http_patch, client_patch = harness.patches()
        with http_patch, client_patch:
            await conn._run_connection()

        assert conn._connection_error is not None
        assert "SilentError" in str(conn._connection_error)
        assert "Connection closed or timed out" in str(conn._connection_error)

    @pytest.mark.asyncio
    async def test_finally_clears_session_and_state(self, conn: HTTPTransportConnection) -> None:
        """The finally block always resets _session, _client_context, and state."""
        harness = _ClientHarness()
        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

        http_patch, client_patch = harness.patches()
        with http_patch, client_patch:
            task = asyncio.create_task(conn._run_connection())
            await conn._session_ready.wait()
            assert conn._session is harness.clients[0].session
            client_context: object = conn._client_context
            assert client_context is harness.clients[0]

            conn._disconnect_event.set()
            await task

        assert conn._session is None
        assert conn._client_context is None
        assert conn.state == ConnectionState.DISCONNECTED
        assert harness.clients[0].exited


# ===========================================================================
# _cleanup_owner_task
# ===========================================================================


class TestHTTPCleanupOwnerTask:
    @pytest.mark.asyncio
    async def test_no_task(self, conn: HTTPTransportConnection) -> None:
        """No-op when _owner_task is None."""
        await conn._cleanup_owner_task()
        assert conn._owner_task is None
        assert conn._disconnect_event is None
        assert conn._session_ready is None

    @pytest.mark.asyncio
    async def test_done_task(self, conn: HTTPTransportConnection) -> None:
        """Already-done task is just set to None."""
        done_task = asyncio.create_task(drain_asyncio_tasks())
        await done_task  # Let it finish
        conn._owner_task = done_task
        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

        await conn._cleanup_owner_task()

        assert conn._owner_task is None
        assert conn._disconnect_event is None
        assert conn._session_ready is None

    @pytest.mark.asyncio
    async def test_running_task_is_cancelled(self, conn: HTTPTransportConnection) -> None:
        """A running task is cancelled if it does not exit after the grace period."""

        async def long_running() -> None:
            await wait_forever()

        task = asyncio.create_task(long_running())
        conn._owner_task = task
        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

        async def mock_wait(tasks: Any, timeout: float | None = None) -> tuple[set[Any], set[Any]]:
            return set(), set(tasks)

        with patch.object(asyncio, "wait", side_effect=mock_wait):
            await conn._cleanup_owner_task()

        assert conn._owner_task is None
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_task_cancel_timeout_warning(self, conn: HTTPTransportConnection) -> None:
        """If the task doesn't cancel within timeout, cleanup logs warning and finishes."""
        # Create a task that is running but will hang after cancel
        release = asyncio.Event()

        async def stubborn() -> None:
            try:
                await wait_forever()
            except asyncio.CancelledError:
                await release.wait()

        task = asyncio.create_task(stubborn())
        conn._owner_task = task
        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

        async def mock_wait(tasks: Any, timeout: float | None = None) -> tuple[set[Any], set[Any]]:
            return set(), set(tasks)

        call_count = 0

        async def mock_wait_for(fut: Any, timeout: float | None = None) -> None:
            nonlocal call_count
            call_count += 1
            raise TimeoutError()

        with (
            patch.object(asyncio, "wait", side_effect=mock_wait),
            patch.object(asyncio, "wait_for", side_effect=mock_wait_for),
        ):
            await conn._cleanup_owner_task()

        assert conn._owner_task is None
        assert conn._disconnect_event is None
        assert conn._session_ready is None
        assert call_count >= 1

        # Clean up the dangling task
        release.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ===========================================================================
# disconnect
# ===========================================================================


class TestHTTPDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_no_event(self, conn: HTTPTransportConnection) -> None:
        """Disconnect when no connection has been made."""
        await conn.disconnect()
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn._owner_task is None

    @pytest.mark.asyncio
    async def test_disconnect_signals_event_and_cleans_up(
        self, conn: HTTPTransportConnection
    ) -> None:
        """disconnect() sets the event and cleans up owner task."""
        event = asyncio.Event()
        conn._disconnect_event = event

        # Simulate an already-done owner task
        done_task = asyncio.create_task(drain_asyncio_tasks())
        await done_task
        conn._owner_task = done_task
        conn._session_ready = asyncio.Event()

        await conn.disconnect()

        assert event.is_set()
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn._owner_task is None
        assert conn._disconnect_event is None
        assert conn._session_ready is None

    @pytest.mark.asyncio
    async def test_disconnect_waits_for_owner_task_to_exit(
        self, conn: HTTPTransportConnection
    ) -> None:
        """disconnect() lets the owner task unwind before falling back to cancellation."""
        task_finished = asyncio.Event()
        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

        async def cooperative_task() -> None:
            assert conn._disconnect_event is not None
            await conn._disconnect_event.wait()
            task_finished.set()

        task = asyncio.create_task(cooperative_task())
        conn._owner_task = task

        await conn.disconnect()

        assert task_finished.is_set()
        assert task.done()
        assert not task.cancelled()
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn._owner_task is None

    @pytest.mark.asyncio
    async def test_full_connect_then_disconnect(self, conn: HTTPTransportConnection) -> None:
        """Integration: connect, verify connected, disconnect, verify disconnected."""
        harness = _ClientHarness()
        http_patch, client_patch = harness.patches()
        with http_patch, client_patch:
            result = await conn.connect()
            assert result is harness.clients[0].session
            assert conn.state == ConnectionState.CONNECTED
            assert conn.is_connected is True

            await conn.disconnect()

        state_after_disconnect: ConnectionState = conn.state
        assert state_after_disconnect == ConnectionState.DISCONNECTED
        assert conn.is_connected is False
        assert conn._owner_task is None
        assert harness.clients[0].exited


# ===========================================================================
# Base class properties exercised through HTTPTransportConnection
# ===========================================================================


class TestHTTPBaseProperties:
    def test_is_connected_requires_both_state_and_session(
        self, conn: HTTPTransportConnection
    ) -> None:
        # State CONNECTED but no session -> not connected
        conn._state = ConnectionState.CONNECTED
        conn._session = None
        assert conn.is_connected is False

        # Session present but wrong state -> not connected
        conn._state = ConnectionState.DISCONNECTED
        conn._session = MagicMock()
        assert conn.is_connected is False

        # Both present -> connected
        conn._state = ConnectionState.CONNECTED
        assert conn.is_connected is True

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self, conn: HTTPTransportConnection) -> None:
        result = await conn.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_connected_success(self, conn: HTTPTransportConnection) -> None:
        mock_session = AsyncMock()
        mock_session.list_tools = AsyncMock(return_value=[])
        conn._state = ConnectionState.CONNECTED
        conn._session = mock_session

        result = await conn.health_check()
        assert result is True
        assert conn._consecutive_failures == 0
        assert conn._last_health_check is not None

    @pytest.mark.asyncio
    async def test_health_check_connected_failure(self, conn: HTTPTransportConnection) -> None:
        mock_session = AsyncMock()
        mock_session.list_tools = AsyncMock(side_effect=TimeoutError("slow"))
        conn._state = ConnectionState.CONNECTED
        conn._session = mock_session

        result = await conn.health_check()
        assert result is False
        assert conn._consecutive_failures == 1
