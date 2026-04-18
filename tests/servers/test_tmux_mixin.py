"""Tests for WebSocket TmuxMixin handlers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from gobby.servers.websocket.server import WebSocketServer

pytestmark = pytest.mark.unit


class MockWebSocket:
    def __init__(self, user_id: str = "test-user") -> None:
        self.user_id = user_id
        self.latency = 0.1
        self.sent_messages: list[str] = []
        self.closed = False
        self.subscriptions: set[str] = {"*"}
        self.remote_address = ("127.0.0.1", 12345)

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    def last_message(self) -> dict:
        return json.loads(self.sent_messages[-1])

    def all_messages(self) -> list[dict]:
        return [json.loads(m) for m in self.sent_messages]

    def messages_of_type(self, msg_type: str) -> list[dict]:
        return [m for m in self.all_messages() if m.get("type") == msg_type]


class DisconnectingWebSocket(MockWebSocket):
    async def send(self, message: str) -> None:
        raise ConnectionClosedOK(Close(1001, "going away"), Close(1001, "going away"), True)


@pytest.fixture
def mock_config() -> MagicMock:
    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    return config


@pytest.fixture
def mock_mcp_manager() -> MagicMock:
    return MagicMock()


@pytest.fixture
def server(mock_config: MagicMock, mock_mcp_manager: MagicMock) -> WebSocketServer:
    return WebSocketServer(mock_config, mock_mcp_manager)


class TestTmuxMixinInit:
    """Test TmuxMixin initialization."""

    def test_tmux_bridge_initialized(self, server: WebSocketServer) -> None:
        assert hasattr(server, "_tmux_bridge")
        assert hasattr(server, "_tmux_mgr_gobby")
        assert hasattr(server, "_tmux_mgr_default")
        assert hasattr(server, "_tmux_client_bridges")

    def test_gobby_manager_has_socket(self, server: WebSocketServer) -> None:
        assert server._tmux_mgr_gobby.config.socket_name == "gobby"

    def test_default_manager_no_socket(self, server: WebSocketServer) -> None:
        assert server._tmux_mgr_default.config.socket_name == ""


class TestTmuxListSessions:
    """Test _handle_tmux_list_sessions handler."""

    @pytest.mark.asyncio
    async def test_list_empty(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        with (
            patch.object(
                server._tmux_mgr_default, "list_sessions", new_callable=AsyncMock, return_value=[]
            ),
            patch.object(
                server._tmux_mgr_gobby, "list_sessions", new_callable=AsyncMock, return_value=[]
            ),
        ):
            await server._handle_tmux_list_sessions(ws, {"request_id": "r1"})

        msg = ws.last_message()
        assert msg["type"] == "tmux_sessions_list"
        assert msg["sessions"] == []
        assert msg["request_id"] == "r1"

    @pytest.mark.asyncio
    async def test_list_with_sessions(self, server: WebSocketServer) -> None:
        from gobby.agents.tmux.session_manager import TmuxSessionInfo

        ws = MockWebSocket()
        default_sessions = [TmuxSessionInfo(name="user-1", pane_pid=100)]
        gobby_sessions = [TmuxSessionInfo(name="agent-1", pane_pid=200)]

        with (
            patch.object(
                server._tmux_mgr_default,
                "list_sessions",
                new_callable=AsyncMock,
                return_value=default_sessions,
            ),
            patch.object(
                server._tmux_mgr_gobby,
                "list_sessions",
                new_callable=AsyncMock,
                return_value=gobby_sessions,
            ),
        ):
            await server._handle_tmux_list_sessions(ws, {})

        msg = ws.last_message()
        assert len(msg["sessions"]) == 2
        assert msg["sessions"][0]["name"] == "user-1"
        assert msg["sessions"][0]["socket"] == "default"
        assert msg["sessions"][0]["pane_pid"] == 100
        assert msg["sessions"][1]["name"] == "agent-1"
        assert msg["sessions"][1]["socket"] == "gobby"

    @pytest.mark.asyncio
    async def test_list_ignores_disconnect_during_response_send(
        self, server: WebSocketServer
    ) -> None:
        ws = DisconnectingWebSocket()
        with (
            patch.object(
                server._tmux_mgr_default, "list_sessions", new_callable=AsyncMock, return_value=[]
            ),
            patch.object(
                server._tmux_mgr_gobby, "list_sessions", new_callable=AsyncMock, return_value=[]
            ),
        ):
            await server._handle_tmux_list_sessions(ws, {"request_id": "r1"})


class TestTmuxAttach:
    """Test _handle_tmux_attach handler."""

    @pytest.mark.asyncio
    async def test_attach_missing_session_name(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        await server._handle_tmux_attach(ws, {"request_id": "r1"})

        errors = ws.messages_of_type("error")
        assert len(errors) == 1
        assert "session_name" in errors[0]["message"].lower() or "Missing" in errors[0]["message"]

    @pytest.mark.asyncio
    async def test_attach_session_not_found(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        with patch.object(
            server._tmux_mgr_default,
            "has_session",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await server._handle_tmux_attach(
                ws, {"request_id": "r1", "session_name": "missing", "socket": "default"}
            )

        errors = ws.messages_of_type("error")
        assert len(errors) == 1
        assert "not found" in errors[0]["message"].lower()


class TestTmuxDetach:
    """Test _handle_tmux_detach handler."""

    @pytest.mark.asyncio
    async def test_detach_missing_streaming_id(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        await server._handle_tmux_detach(ws, {"request_id": "r1"})

        errors = ws.messages_of_type("error")
        assert len(errors) == 1

    @pytest.mark.asyncio
    async def test_detach_success(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        mock_reader = MagicMock()
        mock_reader.stop_reader = AsyncMock()

        with patch("gobby.agents.pty_reader.get_pty_reader_manager", return_value=mock_reader):
            with patch.object(server._tmux_bridge, "detach", new_callable=AsyncMock):
                await server._handle_tmux_detach(
                    ws, {"request_id": "r1", "streaming_id": "test-stream"}
                )

        results = ws.messages_of_type("tmux_detach_result")
        assert len(results) == 1
        assert results[0]["success"] is True


class TestTmuxCreateSession:
    """Test _handle_tmux_create_session handler."""

    @pytest.mark.asyncio
    async def test_create_tmux_not_available(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        with patch.object(server._tmux_mgr_default, "is_available", return_value=False):
            await server._handle_tmux_create_session(ws, {"request_id": "r1"})

        errors = ws.messages_of_type("error")
        assert len(errors) == 1
        assert "not installed" in errors[0]["message"].lower()

    @pytest.mark.asyncio
    async def test_create_success(self, server: WebSocketServer) -> None:
        from gobby.agents.tmux.session_manager import TmuxSessionInfo

        ws = MockWebSocket()
        server.clients[ws] = {"id": "c1", "user_id": "test"}

        with (
            patch.object(server._tmux_mgr_default, "is_available", return_value=True),
            patch.object(
                server._tmux_mgr_default,
                "create_session",
                new_callable=AsyncMock,
                return_value=TmuxSessionInfo(name="new-session", pane_pid=42),
            ),
        ):
            await server._handle_tmux_create_session(
                ws, {"request_id": "r1", "name": "new-session"}
            )

        results = ws.messages_of_type("tmux_create_result")
        assert len(results) == 1
        assert results[0]["success"] is True
        assert results[0]["session_name"] == "new-session"
        assert results[0]["pane_pid"] == 42


class TestTmuxKillSession:
    """Test _handle_tmux_kill_session handler."""

    @pytest.mark.asyncio
    async def test_kill_missing_name(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        await server._handle_tmux_kill_session(ws, {"request_id": "r1"})

        errors = ws.messages_of_type("error")
        assert len(errors) == 1

    @pytest.mark.asyncio
    async def test_kill_agent_managed_refused(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        mock_run = MagicMock()
        mock_run.id = "ar-1"
        mock_run.tmux_session_name = "agent-sess"
        mock_run.mode = "tmux"

        mock_session_mgr = MagicMock()
        mock_arm = MagicMock()
        mock_arm.list_active.return_value = [mock_run]
        server.session_manager = mock_session_mgr

        with patch("gobby.storage.agents.LocalAgentRunManager", return_value=mock_arm):
            await server._handle_tmux_kill_session(
                ws,
                {"request_id": "r1", "session_name": "agent-sess", "socket": "gobby"},
            )

        errors = ws.messages_of_type("error")
        assert len(errors) == 1
        assert errors[0]["code"] == "AGENT_MANAGED"


class TestTmuxResize:
    """Test _handle_tmux_resize handler."""

    @pytest.mark.asyncio
    async def test_resize_missing_fields(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        # Should not raise - silent failure
        await server._handle_tmux_resize(ws, {})

    @pytest.mark.asyncio
    async def test_resize_calls_bridge(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        with patch.object(server._tmux_bridge, "resize", new_callable=AsyncMock) as mock_resize:
            await server._handle_tmux_resize(ws, {"streaming_id": "s1", "rows": 24, "cols": 80})
            mock_resize.assert_called_once_with("s1", 24, 80)


class TestTmuxClientCleanup:
    """Test client disconnect cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_empty(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        await server._cleanup_tmux_client(ws)  # should not raise

    @pytest.mark.asyncio
    async def test_cleanup_with_bridges(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        server._tmux_client_bridges[ws] = {"stream-1", "stream-2"}

        mock_reader = MagicMock()
        mock_reader.stop_reader = AsyncMock()

        with patch("gobby.agents.pty_reader.get_pty_reader_manager", return_value=mock_reader):
            with patch.object(server._tmux_bridge, "detach", new_callable=AsyncMock) as mock_detach:
                await server._cleanup_tmux_client(ws)

            assert mock_detach.call_count == 2

        assert ws not in server._tmux_client_bridges


class TestTerminalInputBridgeRouting:
    """Test terminal_input routes to PTY bridges before agent registry."""

    @pytest.mark.asyncio
    async def test_input_routes_to_bridge(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        with patch.object(
            server._tmux_bridge, "get_master_fd", new_callable=AsyncMock, return_value=42
        ):
            with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
                await server._handle_terminal_input(ws, {"run_id": "tmux-abc123", "data": "ls\n"})
                mock_thread.assert_called_once()
                args = mock_thread.call_args
                assert args[0][1] == 42  # fd
                assert args[0][2] == b"ls\n"  # data

    @pytest.mark.asyncio
    async def test_input_falls_through_to_db_lookup(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        mock_session_mgr = MagicMock()
        server.session_manager = mock_session_mgr
        mock_arm = MagicMock()
        mock_arm.get.return_value = None

        # Bridge returns None for fd - should fall through to DB lookup
        with patch.object(
            server._tmux_bridge, "get_master_fd", new_callable=AsyncMock, return_value=None
        ):
            with patch("gobby.storage.agents.LocalAgentRunManager", return_value=mock_arm):
                await server._handle_terminal_input(ws, {"run_id": "some-agent", "data": "x"})
                mock_arm.get.assert_called_once_with("some-agent")
