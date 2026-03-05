"""Tests for HTTP transport connection.

Exercises the real HTTPTransportConnection code paths. Only the MCP SDK's
streamablehttp_client and ClientSession are mocked (external I/O).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.models import ConnectionState, MCPError, MCPServerConfig
from gobby.mcp_proxy.transports.http import HTTPTransportConnection

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> MCPServerConfig:
    """Create a real MCPServerConfig for HTTP transport."""
    defaults = {
        "name": "test-http",
        "project_id": "proj-001",
        "transport": "http",
        "url": "http://localhost:8080/mcp",
        "headers": {"Authorization": "Bearer tok"},
        "connect_timeout": 2.0,
    }
    defaults.update(overrides)
    return MCPServerConfig(**defaults)


def _mock_session() -> AsyncMock:
    """Create a mock ClientSession with initialize()."""
    session = AsyncMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=[])
    return session


@asynccontextmanager
async def _fake_streamablehttp(url: str, headers: dict | None = None):
    """Async context manager mimicking streamablehttp_client."""
    read = MagicMock()
    write = MagicMock()
    yield read, write, None


@asynccontextmanager
async def _fake_streamablehttp_error(url: str, headers: dict | None = None):
    """streamablehttp_client that raises on entry."""
    raise ConnectionError("refused")
    yield  # noqa: unreachable — needed for generator syntax


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
        assert conn._session_context is None

    def test_config_stored(self, conn: HTTPTransportConnection) -> None:
        assert conn.config.name == "test-http"
        assert conn.config.url == "http://localhost:8080/mcp"

    def test_auth_token_default_none(self, conn: HTTPTransportConnection) -> None:
        assert conn._auth_token is None

    def test_auth_token_set(self, config: MCPServerConfig) -> None:
        c = HTTPTransportConnection(config, auth_token="secret")
        assert c._auth_token == "secret"


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
    @patch("gobby.mcp_proxy.transports.http.ClientSession")
    @patch("gobby.mcp_proxy.transports.http.streamablehttp_client")
    async def test_full_connect_lifecycle(
        self,
        mock_streamable: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: HTTPTransportConnection,
    ) -> None:
        """Test that connect() goes through CONNECTING -> CONNECTED."""
        mock_session = _mock_session()

        # streamablehttp_client is used as `async with streamablehttp_client(...)`
        # which means it returns an async context manager
        mock_read = MagicMock()
        mock_write = MagicMock()

        @asynccontextmanager
        async def fake_client(url, headers=None):
            yield mock_read, mock_write, None

        mock_streamable.side_effect = fake_client

        # ClientSession(read, write) returns context manager yielding session
        mock_session_instance = AsyncMock()
        mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_session_cls.return_value = mock_session_instance

        result = await conn.connect()
        assert result is mock_session
        assert conn.state == ConnectionState.CONNECTED
        assert conn.is_connected is True
        assert conn._consecutive_failures == 0

        # Clean up - signal disconnect so background task can finish
        await conn.disconnect()

    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.http.ClientSession")
    @patch("gobby.mcp_proxy.transports.http.streamablehttp_client")
    async def test_connect_passes_url_and_headers(
        self,
        mock_streamable: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: HTTPTransportConnection,
    ) -> None:
        """Verify url and headers from config are passed to streamablehttp_client."""
        captured_args: dict[str, Any] = {}
        mock_session = _mock_session()

        @asynccontextmanager
        async def capture_client(url, headers=None):
            captured_args["url"] = url
            captured_args["headers"] = headers
            yield MagicMock(), MagicMock(), None

        mock_streamable.side_effect = capture_client

        mock_session_instance = AsyncMock()
        mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_session_cls.return_value = mock_session_instance

        await conn.connect()

        assert captured_args["url"] == "http://localhost:8080/mcp"
        assert captured_args["headers"] == {"Authorization": "Bearer tok"}

        await conn.disconnect()


# ===========================================================================
# connect() — reconnect path (existing _owner_task)
# ===========================================================================


class TestHTTPConnectReconnect:
    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.http.ClientSession")
    @patch("gobby.mcp_proxy.transports.http.streamablehttp_client")
    async def test_reconnect_cleans_old_task(
        self,
        mock_streamable: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: HTTPTransportConnection,
    ) -> None:
        """If _owner_task already exists, connect() calls disconnect() first."""
        mock_session = _mock_session()

        @asynccontextmanager
        async def fake_client(url, headers=None):
            yield MagicMock(), MagicMock(), None

        mock_streamable.side_effect = fake_client

        mock_session_instance = AsyncMock()
        mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_session_cls.return_value = mock_session_instance

        # Simulate an existing owner task that's already done
        old_task = MagicMock()
        old_task.done.return_value = True
        conn._owner_task = old_task

        result = await conn.connect()
        assert result is mock_session
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
            await asyncio.sleep(100)

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
            await asyncio.sleep(100)

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
    @patch("gobby.mcp_proxy.transports.http.streamablehttp_client")
    async def test_connection_exception_wraps_as_mcp_error(
        self,
        mock_streamable: MagicMock,
        conn: HTTPTransportConnection,
    ) -> None:
        """Non-MCPError exceptions get wrapped in MCPError."""

        @asynccontextmanager
        async def failing_client(url, headers=None):
            raise OSError("network down")
            yield  # noqa

        mock_streamable.side_effect = failing_client

        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

        await conn._run_connection()

        assert conn._connection_error is not None
        assert isinstance(conn._connection_error, MCPError)
        assert "network down" in str(conn._connection_error)
        assert conn._session is None
        assert conn.state == ConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.http.streamablehttp_client")
    async def test_mcp_error_not_double_wrapped(
        self,
        mock_streamable: MagicMock,
        conn: HTTPTransportConnection,
    ) -> None:
        """If exception is already MCPError, it's stored directly."""
        original = MCPError("original error")

        @asynccontextmanager
        async def failing_client(url, headers=None):
            raise original
            yield  # noqa

        mock_streamable.side_effect = failing_client

        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

        await conn._run_connection()

        assert conn._connection_error is original

    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.http.streamablehttp_client")
    async def test_empty_error_message_uses_type_name(
        self,
        mock_streamable: MagicMock,
        conn: HTTPTransportConnection,
    ) -> None:
        """Exceptions with empty str() get a type-name-based message."""

        class SilentError(Exception):
            def __str__(self) -> str:
                return ""

        @asynccontextmanager
        async def failing_client(url, headers=None):
            raise SilentError()
            yield  # noqa

        mock_streamable.side_effect = failing_client

        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

        await conn._run_connection()

        assert conn._connection_error is not None
        assert "SilentError" in str(conn._connection_error)
        assert "Connection closed or timed out" in str(conn._connection_error)

    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.http.ClientSession")
    @patch("gobby.mcp_proxy.transports.http.streamablehttp_client")
    async def test_finally_clears_session_and_state(
        self,
        mock_streamable: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: HTTPTransportConnection,
    ) -> None:
        """The finally block always resets _session, _session_context, and state."""
        mock_session = _mock_session()

        @asynccontextmanager
        async def fake_client(url, headers=None):
            yield MagicMock(), MagicMock(), None

        mock_streamable.side_effect = fake_client

        mock_session_instance = AsyncMock()
        mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_session_cls.return_value = mock_session_instance

        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

        # Run _run_connection in a task so we can signal disconnect
        task = asyncio.create_task(conn._run_connection())

        # Wait for session to be ready
        await conn._session_ready.wait()
        assert conn._session is mock_session

        # Signal disconnect
        conn._disconnect_event.set()
        await task

        # After run_connection completes, everything is cleaned up
        assert conn._session is None
        assert conn._session_context is None
        assert conn.state == ConnectionState.DISCONNECTED


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
        done_task = asyncio.create_task(asyncio.sleep(0))
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
        """A running task gets cancelled and awaited."""

        async def long_running() -> None:
            await asyncio.sleep(100)

        task = asyncio.create_task(long_running())
        conn._owner_task = task
        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

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
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                await release.wait()

        task = asyncio.create_task(stubborn())
        conn._owner_task = task
        conn._disconnect_event = asyncio.Event()
        conn._session_ready = asyncio.Event()

        # We need to trigger the TimeoutError path in _cleanup_owner_task.
        # The code does: task.cancel() then await asyncio.wait_for(task, timeout=2.0)
        # We patch asyncio.wait_for at the module level to raise TimeoutError directly.
        call_count = 0

        async def mock_wait_for(fut, timeout=None):
            nonlocal call_count
            call_count += 1
            # The cleanup calls wait_for on the owner task - make it timeout
            raise TimeoutError()

        with patch.object(asyncio, "wait_for", side_effect=mock_wait_for):
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
        done_task = asyncio.create_task(asyncio.sleep(0))
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
    @patch("gobby.mcp_proxy.transports.http.ClientSession")
    @patch("gobby.mcp_proxy.transports.http.streamablehttp_client")
    async def test_full_connect_then_disconnect(
        self,
        mock_streamable: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: HTTPTransportConnection,
    ) -> None:
        """Integration: connect, verify connected, disconnect, verify disconnected."""
        mock_session = _mock_session()

        @asynccontextmanager
        async def fake_client(url, headers=None):
            yield MagicMock(), MagicMock(), None

        mock_streamable.side_effect = fake_client

        mock_session_instance = AsyncMock()
        mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_session_cls.return_value = mock_session_instance

        # Connect
        result = await conn.connect()
        assert result is mock_session
        assert conn.state == ConnectionState.CONNECTED
        assert conn.is_connected is True

        # Disconnect
        await conn.disconnect()
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn.is_connected is False
        assert conn._owner_task is None


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

    def test_set_auth_token(self, conn: HTTPTransportConnection) -> None:
        conn.set_auth_token("new-token")
        assert conn._auth_token == "new-token"

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
