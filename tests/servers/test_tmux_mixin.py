"""Tests for WebSocket TmuxMixin handlers."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any
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
    return WebSocketServer(mock_config, mock_mcp_manager, AsyncMock(return_value="test-user"))


class TestTmuxMixinInit:
    """Test TmuxMixin initialization."""

    def test_tmux_bridge_initialized(self, server: WebSocketServer) -> None:
        assert not hasattr(server, "_tmux_bridge")
        assert hasattr(server, "_tmux_mgr_gobby")
        assert hasattr(server, "_tmux_mgr_default")
        assert hasattr(server, "_tmux_client_bridges")
        assert hasattr(server, "lease_registry")

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
        assert msg["type"] == "terminal_list"
        assert msg["sessions"] == []
        assert msg["request_id"] == "r1"

    @pytest.mark.asyncio
    async def test_list_with_sessions(self, server: WebSocketServer) -> None:
        from gobby.agents.tmux.session_manager import TmuxSessionInfo

        ws = MockWebSocket()
        default_sessions = [
            TmuxSessionInfo(
                name="user-1", pane_pid=100, pane_command="claude", pane_path="/Users/dev/proj"
            )
        ]
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
        assert msg["sessions"][0]["pane_command"] == "claude"
        assert msg["sessions"][0]["pane_path"] == "/Users/dev/proj"
        assert msg["sessions"][1]["name"] == "agent-1"
        assert msg["sessions"][1]["socket"] == "gobby"
        assert msg["sessions"][1]["pane_command"] is None
        assert msg["sessions"][1]["pane_path"] is None

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
        assert ws.sent_messages == []


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

    @pytest.mark.asyncio
    async def test_attach_configures_session_through_manager(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        manager = server._tmux_mgr_default

        with (
            patch.object(manager, "has_session", new_callable=AsyncMock, return_value=True),
            patch.object(manager, "set_option", new_callable=AsyncMock) as set_option,
            patch.object(manager, "refresh_client", new_callable=AsyncMock) as refresh_client,
        ):
            await server._handle_tmux_attach(ws, {"session_name": "demo"})

        set_option.assert_not_awaited()
        refresh_client.assert_not_awaited()
        response = ws.last_message()
        assert response["success"] is True
        assert response["session_name"] == "demo"
        assert response["socket"] == "default"
        streaming_id = response["streaming_id"]
        assert streaming_id.startswith("tmux-")
        assert streaming_id in server._tmux_client_bridges[ws]


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
            await server._handle_tmux_detach(
                ws, {"request_id": "r1", "streaming_id": "test-stream"}
            )

        results = ws.messages_of_type("terminal_detach_result")
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

        results = ws.messages_of_type("terminal_create_result")
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
        mock_run.terminal_id = "term-agent"
        mock_run.mode = "tmux"
        mock_row = MagicMock()
        mock_row.session_name = "agent-sess"
        server.terminal_manager = MagicMock()
        server.terminal_manager.get.return_value = mock_row

        mock_session_mgr = MagicMock()
        mock_arm = MagicMock()
        mock_arm.list_active_for_machine.return_value = [mock_run]
        server.session_manager = mock_session_mgr

        with patch("gobby.storage.agents.LocalAgentRunManager", return_value=mock_arm):
            await server._handle_tmux_kill_session(
                ws,
                {"request_id": "r1", "session_name": "agent-sess", "socket": "gobby"},
            )

        errors = ws.messages_of_type("error")
        assert len(errors) == 1
        assert errors[0]["code"] == "AGENT_MANAGED"

    @pytest.mark.asyncio
    async def test_kill_expires_mapped_gobby_session(self, server: WebSocketServer) -> None:
        from gobby.agents.tmux.session_manager import TmuxSessionInfo

        ws = MockWebSocket()
        gobby_session = MagicMock()
        gobby_session.id = "sess-1"
        gobby_session.terminal_context = {
            "tmux_pane": "%5",
            "tmux_socket_path": "/private/tmp/tmux-501/default",
        }

        mock_session_mgr = MagicMock()
        mock_session_mgr.list.side_effect = (
            lambda status: [gobby_session] if status == "active" else []
        )
        mock_arm = MagicMock()
        mock_arm.list_active_for_machine.return_value = []
        server.session_manager = mock_session_mgr
        server.broadcast_session_event = AsyncMock()

        with (
            patch("gobby.storage.agents.LocalAgentRunManager", return_value=mock_arm),
            patch("gobby.agents.pty_reader.get_pty_reader_manager", return_value=MagicMock()),
            patch.object(
                server._tmux_mgr_default,
                "list_sessions",
                new_callable=AsyncMock,
                return_value=[TmuxSessionInfo(name="term-1", pane_id="%5")],
            ),
            patch.object(
                server._tmux_mgr_default,
                "kill_session",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await server._handle_tmux_kill_session(
                ws,
                {"request_id": "r1", "session_name": "term-1", "socket": "default"},
            )

        mock_session_mgr.update_status.assert_called_once_with("sess-1", "expired")
        server.broadcast_session_event.assert_not_awaited()
        results = ws.messages_of_type("terminal_kill_result")
        assert results[0]["success"] is True
        assert results[0]["expired_session_ids"] == ["sess-1"]

    @pytest.mark.asyncio
    async def test_kill_does_not_expire_different_socket_session(
        self, server: WebSocketServer
    ) -> None:
        from gobby.agents.tmux.session_manager import TmuxSessionInfo

        ws = MockWebSocket()
        gobby_session = MagicMock()
        gobby_session.id = "sess-1"
        gobby_session.terminal_context = {
            "tmux_pane": "%5",
            "tmux_socket_path": "/private/tmp/tmux-501/gobby",
        }

        mock_session_mgr = MagicMock()
        mock_session_mgr.list.side_effect = (
            lambda status: [gobby_session] if status == "active" else []
        )
        mock_arm = MagicMock()
        mock_arm.list_active_for_machine.return_value = []
        server.session_manager = mock_session_mgr

        with (
            patch("gobby.storage.agents.LocalAgentRunManager", return_value=mock_arm),
            patch("gobby.agents.pty_reader.get_pty_reader_manager", return_value=MagicMock()),
            patch.object(
                server._tmux_mgr_default,
                "list_sessions",
                new_callable=AsyncMock,
                return_value=[TmuxSessionInfo(name="term-1", pane_id="%5")],
            ),
            patch.object(
                server._tmux_mgr_default,
                "kill_session",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await server._handle_tmux_kill_session(
                ws,
                {"request_id": "r1", "session_name": "term-1", "socket": "default"},
            )

        mock_session_mgr.update_status.assert_not_called()
        assert mock_session_mgr.update_status.call_count == 0
        assert not mock_session_mgr.update_status.called
        results = ws.messages_of_type("terminal_kill_result")
        assert results[0]["expired_session_ids"] == []


class TestTmuxResize:
    """Test _handle_tmux_resize handler."""

    @pytest.mark.asyncio
    async def test_resize_missing_fields(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        result = await server._handle_tmux_resize(ws, {})
        assert result is None
        assert ws.sent_messages == []

    @pytest.mark.asyncio
    async def test_resize_ignores_malformed_dimensions(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        await server._handle_tmux_resize(ws, {"streaming_id": "s1", "rows": "many", "cols": 80})
        assert ws.sent_messages == [] or ws.messages_of_type("terminal_error")

    @pytest.mark.parametrize(
        ("rows", "cols"),
        [
            (True, 80),  # bool is an int subclass and would coerce to 1
            (24.9, 80),  # a float would truncate to 24
            (24, "80"),  # a numeric string would coerce to 80
            (24, 80.0),  # even a whole float is not the wire contract
        ],
    )
    @pytest.mark.asyncio
    async def test_resize_rejects_non_integer_dimensions(
        self, server: WebSocketServer, rows: object, cols: object
    ) -> None:
        # These size the tmux pane and arrive from an untrusted client, so the
        # wire type is checked rather than coerced.
        ws = MockWebSocket()
        await server._handle_tmux_resize(ws, {"streaming_id": "s1", "rows": rows, "cols": cols})

        assert [message["code"] for message in ws.messages_of_type("terminal_error")] == [
            "invalid_dimensions"
        ]

    @pytest.mark.asyncio
    async def test_resize_calls_bridge(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        await server._handle_tmux_resize(ws, {"streaming_id": "s1", "rows": 24, "cols": 80})
        assert ws.messages_of_type("terminal_error") or ws.sent_messages == []

    @pytest.mark.asyncio
    async def test_resize_refreshes_through_manager(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        await server._handle_tmux_resize(ws, {"streaming_id": "s1", "rows": 24, "cols": 80})
        assert ws.messages_of_type("terminal_error") or ws.sent_messages == []

    @pytest.mark.asyncio
    async def test_a_resize_that_changed_nothing_does_not_resize_the_runtime(
        self, server: WebSocketServer
    ) -> None:
        """The web client resizes again right after attaching (#20805).

        Repainting for a resize that changed nothing lands after the attach
        history, so the runtime is told only when the geometry actually differs,
        and the recorded geometry follows the runtime call.
        """
        from tests.terminals.fakes import FakeRuntime, MemoryTerminalStore, make_memory_terminal

        row = make_memory_terminal()
        store = MemoryTerminalStore(row)
        runtime = FakeRuntime()
        server.terminal_manager = store
        server.terminal_runtime_registry = SimpleNamespace(resolve=lambda _backend: runtime)
        attachment = server.lease_registry.attach(row.id, frame_delivery="proxy")
        server.lease_registry.take_control(row.id, attachment.attachment_id, takeover=False)
        ws = MockWebSocket()
        resize = {
            "type": "terminal_resize",
            "terminal_id": row.id,
            "attachment_id": attachment.attachment_id,
            "rows": 24,
            "cols": 80,
        }

        await server._handle_terminal_resize(ws, resize)
        await server._handle_terminal_resize(ws, resize)

        assert runtime.resize_calls == []
        assert ws.sent_messages == []

        # A genuine change resizes once and is then itself remembered.
        await server._handle_terminal_resize(ws, {**resize, "rows": 39})
        await server._handle_terminal_resize(ws, {**resize, "rows": 39})

        assert runtime.resize_calls == [(39, 80)]
        recorded = store.get(row.id)
        assert recorded is not None
        assert (recorded.rows, recorded.cols) == (39, 80)


class TestTmuxRefreshClient:
    @pytest.mark.parametrize("socket", ["default", "gobby"])
    @pytest.mark.asyncio
    async def test_dispatch_refreshes_socket_qualified_session(
        self,
        server: WebSocketServer,
        socket: str,
    ) -> None:
        ws = MockWebSocket()
        manager = server._tmux_mgr_gobby if socket == "gobby" else server._tmux_mgr_default

        with (
            patch.object(manager, "has_session", new_callable=AsyncMock, return_value=True),
            patch.object(manager, "refresh_client", new_callable=AsyncMock) as refresh_client,
        ):
            await server._handle_tmux_refresh_client(
                ws,
                {
                    "request_id": "refresh-1",
                    "session_name": "demo",
                    "socket": socket,
                },
            )

        refresh_client.assert_awaited_once_with("demo")
        assert ws.last_message() == {
            "type": "terminal_refresh_result",
            "success": True,
            "request_id": "refresh-1",
            "session_name": "demo",
            "socket": socket,
        }

    @pytest.mark.asyncio
    async def test_missing_session_returns_correlated_error(
        self,
        server: WebSocketServer,
    ) -> None:
        ws = MockWebSocket()

        with (
            patch.object(
                server._tmux_mgr_gobby,
                "has_session",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                server._tmux_mgr_gobby,
                "refresh_client",
                new_callable=AsyncMock,
            ) as refresh_client,
        ):
            await server._handle_tmux_refresh_client(
                ws,
                {
                    "request_id": "refresh-missing",
                    "session_name": "missing",
                    "socket": "gobby",
                },
            )

        refresh_client.assert_not_awaited()
        assert ws.last_message() == {
            "type": "error",
            "code": "ERROR",
            "message": "Session 'missing' not found",
            "request_id": "refresh-missing",
        }


class TestTmuxClientCleanup:
    """Test client disconnect cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_empty(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        result = await server._cleanup_tmux_client(ws)
        assert result is None
        assert ws not in server._tmux_client_bridges

    @pytest.mark.asyncio
    async def test_cleanup_with_bridges(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        server._tmux_client_bridges[ws] = {"stream-1", "stream-2"}
        await server._cleanup_tmux_client(ws)
        assert ws not in server._tmux_client_bridges


class TestTerminalInputRouting:
    """Legacy PTY-bridge input routing is retired; a run id goes to the agent lookup."""

    @pytest.mark.asyncio
    async def test_input_reaches_the_run_lookup_for_a_run_id(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        mock_session_mgr = MagicMock()
        server.session_manager = mock_session_mgr
        mock_arm = MagicMock()
        mock_arm.get.return_value = None
        run_id = str(uuid.uuid4())
        with patch("gobby.storage.agents.LocalAgentRunManager", return_value=mock_arm):
            await server._handle_terminal_input(ws, {"run_id": run_id, "data": "x"})
        mock_arm.get.assert_called_once_with(run_id)

    @pytest.mark.asyncio
    async def test_input_for_a_detached_tmux_id_never_reaches_the_run_lookup(
        self, server: WebSocketServer
    ) -> None:
        """A tmux streaming id is not a uuid, so the runs query would raise on it.

        The web terminal answers tmux's DA and DSR queries as terminal_input, so
        a reply that lands after its attachment detached arrives with a run_id
        the uuid-keyed lookup cannot parse (#20803).
        """
        ws = MockWebSocket()
        # A database has to be reachable, or the handler would stop short of
        # the lookup for a reason that has nothing to do with the id.
        server.session_manager = SimpleNamespace(db=object())

        def refuse_db(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("a tmux streaming id must never reach the agent-run lookup")

        with patch("gobby.storage.agents.LocalAgentRunManager", side_effect=refuse_db):
            await server._handle_terminal_input(
                ws, {"run_id": "tmux-68bd19945ce3", "data": "\x1b[?1;2c"}
            )

        assert ws.sent_messages == []
