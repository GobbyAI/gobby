"""Tests for WebSocket TmuxMixin handlers."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from gobby.agents.tmux.history import HistoryCapture, HistoryCaptureError
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


class QueuedWebSocket(MockWebSocket):
    """A websocket whose inbound frames are all available at once.

    Iterating it yields every queued frame without awaiting anything, so the
    connection loop -- not the transport -- is the only thing that can be
    serializing message handling.
    """

    def __init__(self) -> None:
        super().__init__()
        self.inbound: list[str] = []

    def queue(self, *frames: dict[str, Any]) -> None:
        self.inbound.extend(json.dumps(frame) for frame in frames)

    async def __aiter__(self) -> AsyncIterator[str]:
        for frame in self.inbound:
            yield frame


class TracingWebSocket(MockWebSocket):
    """Records each frame type into a shared call trace."""

    def __init__(self, calls: list[str]) -> None:
        super().__init__()
        self._calls = calls

    async def send(self, message: str) -> None:
        await super().send(message)
        self._calls.append(f"send:{json.loads(message)['type']}")


class HistorySendFailureWebSocket(MockWebSocket):
    """Accepts the attach ack, then dies before the history frame lands."""

    async def send(self, message: str) -> None:
        if json.loads(message).get("type") == "terminal_attach_history":
            raise ConnectionClosedOK(Close(1001, "going away"), Close(1001, "going away"), True)
        await super().send(message)


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


def make_bridge(
    session_name: str = "demo",
    socket_name: str = "",
    pid: int = 9001,
    returncode: int | None = None,
) -> Any:
    """A BridgeInfo stand-in with a controllable client process."""
    return SimpleNamespace(
        session_name=session_name,
        socket_name=socket_name,
        master_fd=42,
        proc=SimpleNamespace(pid=pid, returncode=returncode),
    )


@dataclass
class ActivationHarness:
    """Every boundary the attach state machine reaches, mocked."""

    attach: AsyncMock
    get_bridge: AsyncMock
    list_bridges: AsyncMock
    detach: AsyncMock
    reader: MagicMock
    wait_for_client: AsyncMock
    capture: AsyncMock
    set_option: AsyncMock
    refresh_client: AsyncMock
    has_session: AsyncMock
    bridge: Any
    calls: list[str]


@contextlib.contextmanager
def activation_harness(
    server: WebSocketServer,
    *,
    socket: str = "default",
    session_name: str = "demo",
    bridge: Any | None = None,
    bridges: dict[str, Any] | None = None,
    calls: list[str] | None = None,
) -> Iterator[ActivationHarness]:
    manager = server._tmux_mgr_gobby if socket == "gobby" else server._tmux_mgr_default
    live_bridge = bridge or make_bridge(
        session_name=session_name, socket_name="gobby" if socket == "gobby" else ""
    )
    if calls is None:
        calls = []

    reader = MagicMock()
    reader.start_reader = AsyncMock(return_value=True)
    reader.stop_reader = AsyncMock(return_value=True)

    async def record_attach(**_kwargs: Any) -> int:
        calls.append("attach")
        return 42

    async def record_capture(*_args: Any, **_kwargs: Any) -> HistoryCapture:
        calls.append("capture")
        return HistoryCapture(text="hist", truncated=False, dropped_bytes=0, total_bytes=4)

    async def record_start_reader(*_args: Any, **_kwargs: Any) -> bool:
        calls.append("start_reader")
        return True

    async def record_refresh(*_args: Any, **_kwargs: Any) -> None:
        calls.append("refresh_client")

    async def record_list_bridges() -> dict[str, Any]:
        calls.append("list_bridges")
        return dict(bridges or {})

    reader.start_reader.side_effect = record_start_reader

    with contextlib.ExitStack() as stack:
        harness = ActivationHarness(
            attach=stack.enter_context(
                patch.object(
                    server._tmux_bridge, "attach", new_callable=AsyncMock, side_effect=record_attach
                )
            ),
            get_bridge=stack.enter_context(
                patch.object(
                    server._tmux_bridge,
                    "get_bridge",
                    new_callable=AsyncMock,
                    return_value=live_bridge,
                )
            ),
            list_bridges=stack.enter_context(
                patch.object(
                    server._tmux_bridge,
                    "list_bridges",
                    new_callable=AsyncMock,
                    side_effect=record_list_bridges,
                )
            ),
            detach=stack.enter_context(
                patch.object(server._tmux_bridge, "detach", new_callable=AsyncMock)
            ),
            reader=reader,
            wait_for_client=stack.enter_context(
                patch(
                    "gobby.servers.websocket.tmux_activation._wait_for_client",
                    new_callable=AsyncMock,
                    return_value=True,
                )
            ),
            capture=stack.enter_context(
                patch(
                    "gobby.servers.websocket.tmux_activation.capture_history",
                    new_callable=AsyncMock,
                    side_effect=record_capture,
                )
            ),
            set_option=stack.enter_context(
                patch.object(manager, "set_option", new_callable=AsyncMock)
            ),
            refresh_client=stack.enter_context(
                patch.object(
                    manager, "refresh_client", new_callable=AsyncMock, side_effect=record_refresh
                )
            ),
            has_session=stack.enter_context(
                patch.object(manager, "has_session", new_callable=AsyncMock, return_value=True)
            ),
            bridge=live_bridge,
            calls=calls,
        )
        stack.enter_context(
            patch("gobby.agents.pty_reader.get_pty_reader_manager", return_value=reader)
        )
        yield harness


async def reserve(
    server: WebSocketServer,
    websocket: Any,
    *,
    session_name: str = "demo",
    socket: str = "default",
) -> str:
    """Run the attach half and return the reserved streaming id."""
    manager = server._tmux_mgr_gobby if socket == "gobby" else server._tmux_mgr_default
    with patch.object(manager, "has_session", new_callable=AsyncMock, return_value=True):
        await server._handle_tmux_attach(
            websocket, {"session_name": session_name, "socket": socket}
        )
    result = websocket.messages_of_type("tmux_attach_result")[-1]
    return str(result["streaming_id"])


async def resize(
    server: WebSocketServer,
    websocket: Any,
    streaming_id: str,
    rows: int = 40,
    cols: int = 120,
) -> None:
    await server._handle_tmux_resize(
        websocket, {"streaming_id": streaming_id, "rows": rows, "cols": cols}
    )


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
    async def test_attach_reserves_without_building_anything(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        reader = MagicMock(start_reader=AsyncMock(), stop_reader=AsyncMock())
        manager = server._tmux_mgr_default

        with (
            patch.object(manager, "has_session", new_callable=AsyncMock, return_value=True),
            patch.object(manager, "set_option", new_callable=AsyncMock) as set_option,
            patch.object(manager, "refresh_client", new_callable=AsyncMock) as refresh_client,
            patch.object(server._tmux_bridge, "attach", new_callable=AsyncMock) as attach,
            patch("gobby.agents.pty_reader.get_pty_reader_manager", return_value=reader),
        ):
            await server._handle_tmux_attach(ws, {"session_name": "demo"})

        # Nothing is built until the client's first resize reports geometry.
        attach.assert_not_awaited()
        set_option.assert_not_awaited()
        refresh_client.assert_not_awaited()
        reader.start_reader.assert_not_awaited()

        response = ws.last_message()
        assert response["success"] is True
        assert response["session_name"] == "demo"
        assert response["socket"] == "default"
        streaming_id = response["streaming_id"]
        assert streaming_id.startswith("tmux-")
        assert streaming_id in server._tmux_pending
        assert server._tmux_client_bridges.get(ws) is None

    @pytest.mark.asyncio
    async def test_attach_result_is_the_only_frame_sent(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        await reserve(server, ws)

        assert [message["type"] for message in ws.all_messages()] == ["tmux_attach_result"]

    @pytest.mark.asyncio
    async def test_duplicate_attach_cancels_the_prior_reservation(
        self, server: WebSocketServer
    ) -> None:
        ws = MockWebSocket()
        first = await reserve(server, ws)
        second = await reserve(server, ws)

        assert first not in server._tmux_pending
        assert list(server._tmux_pending) == [second]

    @pytest.mark.asyncio
    async def test_a_second_client_keeps_its_own_reservation(self, server: WebSocketServer) -> None:
        first_ws = MockWebSocket()
        second_ws = MockWebSocket()
        first = await reserve(server, first_ws)
        second = await reserve(server, second_ws)

        assert set(server._tmux_pending) == {first, second}


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
            patch.object(
                server._tmux_bridge, "list_bridges", new_callable=AsyncMock, return_value={}
            ),
        ):
            await server._handle_tmux_kill_session(
                ws,
                {"request_id": "r1", "session_name": "term-1", "socket": "default"},
            )

        mock_session_mgr.update_status.assert_called_once_with("sess-1", "expired")
        server.broadcast_session_event.assert_not_awaited()
        results = ws.messages_of_type("tmux_kill_result")
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
            patch.object(
                server._tmux_bridge, "list_bridges", new_callable=AsyncMock, return_value={}
            ),
        ):
            await server._handle_tmux_kill_session(
                ws,
                {"request_id": "r1", "session_name": "term-1", "socket": "default"},
            )

        mock_session_mgr.update_status.assert_not_called()
        assert mock_session_mgr.update_status.call_count == 0
        assert not mock_session_mgr.update_status.called
        results = ws.messages_of_type("tmux_kill_result")
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
        with patch.object(server._tmux_bridge, "resize", new_callable=AsyncMock) as mock_resize:
            await server._handle_tmux_resize(ws, {"streaming_id": "s1", "rows": "many", "cols": 80})

        mock_resize.assert_not_awaited()
        assert ws.sent_messages == []

    @pytest.mark.parametrize(
        "rows,cols",
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
        with patch.object(server._tmux_bridge, "resize", new_callable=AsyncMock) as mock_resize:
            await server._handle_tmux_resize(ws, {"streaming_id": "s1", "rows": rows, "cols": cols})

        mock_resize.assert_not_awaited()
        assert ws.sent_messages == []

    @pytest.mark.asyncio
    async def test_resize_calls_bridge(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        with patch.object(server._tmux_bridge, "resize", new_callable=AsyncMock) as mock_resize:
            await server._handle_tmux_resize(ws, {"streaming_id": "s1", "rows": 24, "cols": 80})
            mock_resize.assert_called_once_with("s1", 24, 80)
            assert mock_resize.call_count == 1
            assert mock_resize.call_args is not None

    @pytest.mark.asyncio
    async def test_resize_refreshes_through_manager(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        bridge = MagicMock(socket_name="gobby", session_name="demo")

        with (
            patch.object(
                server._tmux_bridge, "resize", new_callable=AsyncMock, return_value=bridge
            ),
            patch.object(
                server._tmux_mgr_gobby, "refresh_client", new_callable=AsyncMock
            ) as refresh_client,
        ):
            await server._handle_tmux_resize(ws, {"streaming_id": "s1", "rows": 24, "cols": 80})

        refresh_client.assert_awaited_once_with("demo")
        assert ws.sent_messages == []


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
            await server._handle_message(
                ws,
                json.dumps(
                    {
                        "type": "tmux_refresh_client",
                        "request_id": "refresh-1",
                        "session_name": "demo",
                        "socket": socket,
                    }
                ),
            )

        refresh_client.assert_awaited_once_with("demo")
        assert ws.last_message() == {
            "type": "tmux_refresh_result",
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
            await server._handle_message(
                ws,
                json.dumps(
                    {
                        "type": "tmux_refresh_client",
                        "request_id": "refresh-missing",
                        "session_name": "missing",
                        "socket": "gobby",
                    }
                ),
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
                assert mock_arm.get.call_count == 1
                assert mock_arm.get.call_args is not None


class TestTmuxActivation:
    """Test the reserve-then-activate state machine driven by the first resize."""

    @pytest.mark.asyncio
    async def test_first_resize_activates_in_order(self, server: WebSocketServer) -> None:
        calls: list[str] = []
        ws = TracingWebSocket(calls)
        streaming_id = await reserve(server, ws)

        with activation_harness(server, calls=calls) as harness:
            await resize(server, ws, streaming_id, rows=40, cols=120)

        assert calls == [
            "send:tmux_attach_result",
            "list_bridges",
            "attach",
            "capture",
            "send:terminal_attach_history",
            "start_reader",
            "refresh_client",
        ]
        assert harness.attach.await_args is not None
        assert harness.attach.await_args.kwargs["rows"] == 40
        assert harness.attach.await_args.kwargs["cols"] == 120
        harness.set_option.assert_any_await("demo", "status", "off")
        harness.set_option.assert_any_await("demo", "mouse", "on")

        history = ws.messages_of_type("terminal_attach_history")[0]
        assert history["streaming_id"] == streaming_id
        assert history["text"] == "hist"
        assert history["truncated"] is False
        assert history["unavailable"] is False
        assert history["total_bytes"] == 4

        assert streaming_id not in server._tmux_pending
        assert streaming_id in server._tmux_client_bridges[ws]

    @pytest.mark.asyncio
    async def test_history_is_serialized_without_ascii_escaping(
        self, server: WebSocketServer
    ) -> None:
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws)

        with activation_harness(server) as harness:
            harness.capture.side_effect = None
            harness.capture.return_value = HistoryCapture(
                text="│ ok", truncated=True, dropped_bytes=7, total_bytes=99
            )
            await resize(server, ws, streaming_id)

        raw = ws.sent_messages[-1]
        assert "│" in raw
        assert "\\u2502" not in raw
        history = ws.messages_of_type("terminal_attach_history")[0]
        assert history["truncated"] is True
        assert history["dropped_bytes"] == 7
        assert history["total_bytes"] == 99

    @pytest.mark.asyncio
    async def test_second_resize_starts_no_second_reader_or_history(
        self, server: WebSocketServer
    ) -> None:
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws)

        with activation_harness(server) as harness:
            await resize(server, ws, streaming_id)
            await resize(server, ws, streaming_id, rows=41, cols=121)

        harness.attach.assert_awaited_once()
        assert harness.reader.start_reader.await_count == 1
        assert len(ws.messages_of_type("terminal_attach_history")) == 1

    @pytest.mark.asyncio
    async def test_resize_from_a_foreign_socket_is_rejected(self, server: WebSocketServer) -> None:
        owner = MockWebSocket()
        intruder = MockWebSocket()
        streaming_id = await reserve(server, owner)

        with activation_harness(server) as harness:
            await resize(server, intruder, streaming_id)

        harness.attach.assert_not_awaited()
        assert streaming_id in server._tmux_pending

    @pytest.mark.parametrize(
        ("rows", "cols"),
        [(0, 80), (24, 0), (-1, 80), (1001, 80), (24, 2001), (24, 10**9)],
    )
    @pytest.mark.asyncio
    async def test_out_of_bound_dimensions_build_nothing(
        self, server: WebSocketServer, rows: int, cols: int
    ) -> None:
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws)

        with activation_harness(server) as harness:
            await resize(server, ws, streaming_id, rows=rows, cols=cols)

        harness.attach.assert_not_awaited()
        assert streaming_id in server._tmux_pending

    @pytest.mark.asyncio
    async def test_capture_failure_with_a_live_stream_degrades(
        self, server: WebSocketServer
    ) -> None:
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws)

        with activation_harness(server) as harness:
            harness.capture.side_effect = HistoryCaptureError("capture-pane timed out")
            await resize(server, ws, streaming_id)

        history = ws.messages_of_type("terminal_attach_history")[0]
        assert history["unavailable"] is True
        assert history["text"] == ""
        assert history["truncated"] is False
        # Losing scrollback must not cost the user a working terminal.
        assert harness.reader.start_reader.await_count == 1
        assert ws.messages_of_type("tmux_activation_failed") == []

    @pytest.mark.asyncio
    async def test_capture_failure_with_a_missing_session_fails_activation(
        self, server: WebSocketServer
    ) -> None:
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws)

        with activation_harness(server) as harness:
            harness.capture.side_effect = HistoryCaptureError("no such session")
            harness.has_session.return_value = False
            await resize(server, ws, streaming_id)

        failures = ws.messages_of_type("tmux_activation_failed")
        assert [failure["code"] for failure in failures] == ["session_missing"]
        assert failures[0]["streaming_id"] == streaming_id
        assert ws.messages_of_type("terminal_attach_history") == []
        harness.reader.start_reader.assert_not_awaited()
        harness.detach.assert_any_await(streaming_id)
        assert streaming_id not in server._tmux_pending

    @pytest.mark.asyncio
    async def test_bridge_process_exiting_during_capture_fails_activation(
        self, server: WebSocketServer
    ) -> None:
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws)

        with activation_harness(server) as harness:

            async def capture_then_die(*_args: Any, **_kwargs: Any) -> HistoryCapture:
                harness.bridge.proc.returncode = 1
                return HistoryCapture(text="hist", truncated=False, dropped_bytes=0, total_bytes=4)

            harness.capture.side_effect = capture_then_die
            await resize(server, ws, streaming_id)

        failures = ws.messages_of_type("tmux_activation_failed")
        assert [failure["code"] for failure in failures] == ["bridge_exited"]
        assert ws.messages_of_type("terminal_attach_history") == []
        harness.reader.start_reader.assert_not_awaited()
        harness.detach.assert_any_await(streaming_id)

    @pytest.mark.asyncio
    async def test_bridge_creation_failure_fails_activation(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws)

        with activation_harness(server) as harness:
            harness.attach.side_effect = RuntimeError("no pty available")
            await resize(server, ws, streaming_id)

        failures = ws.messages_of_type("tmux_activation_failed")
        assert [failure["code"] for failure in failures] == ["bridge_failed"]
        assert "no pty available" in failures[0]["message"]
        assert streaming_id not in server._tmux_pending
        assert streaming_id not in server._tmux_client_bridges.get(ws, set())

    @pytest.mark.asyncio
    async def test_cancel_while_probing_the_session_sends_no_history(
        self, server: WebSocketServer
    ) -> None:
        # The degraded path probes has_session before deciding to degrade. That
        # probe is an await, and its answer describes the moment before it: a
        # kill landing while it is suspended must still stop the history frame.
        ws = MockWebSocket()
        killer = MockWebSocket()
        server.session_manager = None
        streaming_id = await reserve(server, ws)
        gate = asyncio.Event()
        reached = asyncio.Event()

        with activation_harness(server) as harness:
            harness.capture.side_effect = HistoryCaptureError("capture timed out")

            async def blocking_has_session(*_args: Any, **_kwargs: Any) -> bool:
                reached.set()
                await gate.wait()
                return True

            harness.has_session.side_effect = blocking_has_session
            task = asyncio.create_task(resize(server, ws, streaming_id))
            await asyncio.wait_for(reached.wait(), timeout=5)
            with patch.object(
                server._tmux_mgr_default, "kill_session", new_callable=AsyncMock, return_value=True
            ):
                await server._handle_tmux_kill_session(
                    killer, {"session_name": "demo", "socket": "default"}
                )
            gate.set()
            await asyncio.wait_for(task, timeout=5)

        assert ws.messages_of_type("terminal_attach_history") == []
        harness.reader.start_reader.assert_not_awaited()
        harness.detach.assert_any_await(streaming_id)
        assert streaming_id not in server._tmux_pending

    @pytest.mark.asyncio
    async def test_bridge_dying_during_refresh_fails_activation(
        self, server: WebSocketServer
    ) -> None:
        # refresh_client is the last await, and finalizing drops the
        # reservation teardown keys on -- so a client that died during it must
        # not be finalized as a live stream.
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws)
        bridge = make_bridge()

        with activation_harness(server, bridge=bridge) as harness:

            async def kill_during_refresh(*_args: Any, **_kwargs: Any) -> None:
                bridge.proc.returncode = 1

            harness.refresh_client.side_effect = kill_during_refresh
            await resize(server, ws, streaming_id)

        failures = ws.messages_of_type("tmux_activation_failed")
        assert [failure["code"] for failure in failures] == ["bridge_exited"]
        assert len(ws.messages_of_type("terminal_attach_history")) == 1
        harness.detach.assert_any_await(streaming_id)
        assert streaming_id not in server._tmux_pending
        assert streaming_id not in server._tmux_client_bridges.get(ws, set())

    @pytest.mark.asyncio
    async def test_reader_refusing_to_start_fails_activation(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws)

        with activation_harness(server) as harness:
            harness.reader.start_reader = AsyncMock(return_value=False)
            await resize(server, ws, streaming_id)

        failures = ws.messages_of_type("tmux_activation_failed")
        assert [failure["code"] for failure in failures] == ["reader_failed"]
        # History still went out before the reader was asked to start.
        assert len(ws.messages_of_type("terminal_attach_history")) == 1
        harness.detach.assert_any_await(streaming_id)
        assert streaming_id not in server._tmux_pending

    @pytest.mark.asyncio
    async def test_client_registration_timeout_fails_activation(
        self, server: WebSocketServer
    ) -> None:
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws)

        with activation_harness(server) as harness:
            harness.wait_for_client.return_value = False
            await resize(server, ws, streaming_id)

        failures = ws.messages_of_type("tmux_activation_failed")
        assert [failure["code"] for failure in failures] == ["client_registration_failed"]
        harness.capture.assert_not_awaited()
        harness.detach.assert_any_await(streaming_id)
        assert streaming_id not in server._tmux_pending

    @pytest.mark.asyncio
    async def test_history_send_failure_cleans_up_without_a_second_frame(
        self, server: WebSocketServer
    ) -> None:
        ws = HistorySendFailureWebSocket()
        streaming_id = await reserve(server, ws)

        with activation_harness(server) as harness:
            await resize(server, ws, streaming_id)

        assert [message["type"] for message in ws.all_messages()] == ["tmux_attach_result"]
        harness.reader.start_reader.assert_not_awaited()
        harness.detach.assert_any_await(streaming_id)
        assert streaming_id not in server._tmux_pending

    @pytest.mark.asyncio
    async def test_server_stop_during_capture_aborts_activation(
        self, server: WebSocketServer
    ) -> None:
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws)
        gate = asyncio.Event()
        reached = asyncio.Event()

        with activation_harness(server) as harness:

            async def blocking_capture(*_args: Any, **_kwargs: Any) -> HistoryCapture:
                reached.set()
                await gate.wait()
                return HistoryCapture(text="hist", truncated=False, dropped_bytes=0, total_bytes=4)

            harness.capture.side_effect = blocking_capture
            task = asyncio.create_task(resize(server, ws, streaming_id))
            await asyncio.wait_for(reached.wait(), timeout=5)
            await server._cleanup_tmux()
            gate.set()
            await task

        assert ws.messages_of_type("terminal_attach_history") == []
        assert ws.messages_of_type("tmux_activation_failed") == []
        harness.reader.start_reader.assert_not_awaited()
        harness.detach.assert_any_await(streaming_id)
        assert streaming_id not in server._tmux_pending

    @pytest.mark.asyncio
    async def test_cross_socket_kill_during_capture_aborts_activation(
        self, server: WebSocketServer
    ) -> None:
        ws = MockWebSocket()
        killer = MockWebSocket()
        server.session_manager = None
        streaming_id = await reserve(server, ws)
        gate = asyncio.Event()
        reached = asyncio.Event()

        with activation_harness(server) as harness:

            async def blocking_capture(*_args: Any, **_kwargs: Any) -> HistoryCapture:
                reached.set()
                await gate.wait()
                return HistoryCapture(text="hist", truncated=False, dropped_bytes=0, total_bytes=4)

            harness.capture.side_effect = blocking_capture
            task = asyncio.create_task(resize(server, ws, streaming_id))
            await asyncio.wait_for(reached.wait(), timeout=5)
            with patch.object(
                server._tmux_mgr_default, "kill_session", new_callable=AsyncMock, return_value=True
            ):
                await server._handle_tmux_kill_session(
                    killer, {"session_name": "demo", "socket": "default"}
                )
            gate.set()
            await task

        assert ws.messages_of_type("terminal_attach_history") == []
        harness.reader.start_reader.assert_not_awaited()
        harness.detach.assert_any_await(streaming_id)
        assert streaming_id not in server._tmux_pending

    @pytest.mark.asyncio
    async def test_same_socket_detach_queues_behind_activation(
        self, server: WebSocketServer
    ) -> None:
        # Both frames are handed to the real connection loop at once, and the
        # capture is suspended mid-activation. Activation is awaited inline on
        # the connection task, so its correctness depends on the loop finishing
        # one message before pulling the next -- driving _handle_tmux_detach
        # directly could not tell serialized dispatch from concurrent dispatch.
        ws = QueuedWebSocket()
        streaming_id = await reserve(server, ws)
        ws.queue(
            {"type": "tmux_resize", "streaming_id": streaming_id, "rows": 40, "cols": 120},
            {"type": "tmux_detach", "streaming_id": streaming_id},
        )
        gate = asyncio.Event()
        reached = asyncio.Event()

        with activation_harness(server) as harness:

            async def blocking_capture(*_args: Any, **_kwargs: Any) -> HistoryCapture:
                reached.set()
                await gate.wait()
                return HistoryCapture(text="hist", truncated=False, dropped_bytes=0, total_bytes=4)

            harness.capture.side_effect = blocking_capture
            task = asyncio.create_task(server.handle_connection(ws))
            await asyncio.wait_for(reached.wait(), timeout=5)

            # The detach frame is already queued and the loop is free to read
            # it; it stays unhandled because activation has not returned.
            assert ws.messages_of_type("tmux_detach_result") == []
            assert ws.messages_of_type("terminal_attach_history") == []

            gate.set()
            await asyncio.wait_for(task, timeout=5)

        # Activation ran to completion first, then the detach tore it down.
        assert len(ws.messages_of_type("terminal_attach_history")) == 1
        assert ws.messages_of_type("tmux_detach_result")[0]["success"] is True
        assert ws.messages_of_type("error") == []
        harness.detach.assert_any_await(streaming_id)
        harness.reader.stop_reader.assert_any_await(streaming_id)
        assert streaming_id not in server._tmux_client_bridges.get(ws, set())

    @pytest.mark.asyncio
    async def test_client_disconnect_drops_an_unactivated_reservation(
        self, server: WebSocketServer
    ) -> None:
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws)

        await server._cleanup_tmux_client(ws)

        assert streaming_id not in server._tmux_pending


class TestTmuxAttachmentReap:
    """Test that a re-attach never leaves two tmux clients on one session."""

    @pytest.mark.asyncio
    async def test_activation_excludes_itself_from_the_reap(self, server: WebSocketServer) -> None:
        from gobby.servers.websocket.tmux_activation import PendingAttachment

        ws = MockWebSocket()
        streaming_id = await reserve(server, ws)
        server._tmux_pending["stale-reservation"] = PendingAttachment(
            session_name="demo", socket="default", owner=ws
        )
        server._tmux_client_bridges[ws] = {"stale-bridge"}

        with activation_harness(server, bridges={"stale-bridge": make_bridge()}) as harness:
            await resize(server, ws, streaming_id)

        harness.detach.assert_any_await("stale-bridge")
        assert "stale-reservation" not in server._tmux_pending
        # The activating attachment survived the reap and ran to completion.
        assert len(ws.messages_of_type("terminal_attach_history")) == 1
        assert streaming_id not in server._tmux_pending
        assert server._tmux_client_bridges[ws] == {streaming_id}

    @pytest.mark.asyncio
    async def test_a_live_second_viewers_bridge_survives(self, server: WebSocketServer) -> None:
        owner = MockWebSocket()
        other = MockWebSocket()
        streaming_id = await reserve(server, owner)
        server._tmux_client_bridges[other] = {"other-bridge"}

        with activation_harness(server, bridges={"other-bridge": make_bridge()}) as harness:
            await resize(server, owner, streaming_id)

        detached = [await_call.args[0] for await_call in harness.detach.await_args_list]
        assert "other-bridge" not in detached
        assert server._tmux_client_bridges[other] == {"other-bridge"}

    @pytest.mark.asyncio
    async def test_a_closed_owners_bridge_is_reaped(self, server: WebSocketServer) -> None:
        owner = MockWebSocket()
        gone = MockWebSocket()
        gone.closed = True
        streaming_id = await reserve(server, owner)
        server._tmux_client_bridges[gone] = {"orphaned-bridge"}

        with activation_harness(server, bridges={"orphaned-bridge": make_bridge()}) as harness:
            await resize(server, owner, streaming_id)

        harness.detach.assert_any_await("orphaned-bridge")
        assert server._tmux_client_bridges[gone] == set()

    @pytest.mark.asyncio
    async def test_an_unowned_bridge_is_reaped(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws)

        with activation_harness(server, bridges={"orphan": make_bridge()}) as harness:
            await resize(server, ws, streaming_id)

        harness.detach.assert_any_await("orphan")
        assert all(
            "orphan" not in bridge_ids for bridge_ids in server._tmux_client_bridges.values()
        )
        assert len(ws.messages_of_type("terminal_attach_history")) == 1

    @pytest.mark.asyncio
    async def test_default_socket_bridges_match_despite_empty_socket_name(
        self, server: WebSocketServer
    ) -> None:
        # BridgeInfo.socket_name carries the config value ("" for the user's
        # default server) while the wire says "default".
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws, socket="default")
        server._tmux_client_bridges[ws] = {"stale-default"}

        with activation_harness(
            server, bridges={"stale-default": make_bridge(socket_name="")}
        ) as harness:
            await resize(server, ws, streaming_id)

        harness.detach.assert_any_await("stale-default")
        assert server._tmux_client_bridges[ws] == {streaming_id}
        assert len(ws.messages_of_type("terminal_attach_history")) == 1

    @pytest.mark.asyncio
    async def test_a_bridge_on_the_other_socket_is_left_alone(
        self, server: WebSocketServer
    ) -> None:
        ws = MockWebSocket()
        streaming_id = await reserve(server, ws, socket="default")
        server._tmux_client_bridges[ws] = {"gobby-bridge"}

        with activation_harness(
            server, bridges={"gobby-bridge": make_bridge(socket_name="gobby")}
        ) as harness:
            await resize(server, ws, streaming_id)

        detached = [await_call.args[0] for await_call in harness.detach.await_args_list]
        assert "gobby-bridge" not in detached

    @pytest.mark.asyncio
    async def test_a_stale_reservation_cannot_build_a_second_client(
        self, server: WebSocketServer
    ) -> None:
        ws = MockWebSocket()
        stale = await reserve(server, ws)
        fresh = await reserve(server, ws)

        with activation_harness(server) as harness:
            # The duplicate attach already cancelled the older reservation, so
            # a resize naming it finds nothing to activate.
            await resize(server, ws, stale)
            harness.attach.assert_not_awaited()
            assert stale not in server._tmux_pending
            assert ws.messages_of_type("terminal_attach_history") == []
            await resize(server, ws, fresh)

        harness.attach.assert_awaited_once()
        assert len(ws.messages_of_type("terminal_attach_history")) == 1
        assert server._tmux_client_bridges[ws] == {fresh}


class TestTmuxClientRegistration:
    """Test the poll that proves tmux applied our client's geometry."""

    @pytest.mark.asyncio
    async def test_matches_our_attach_process_pid(self, server: WebSocketServer) -> None:
        from gobby.servers.websocket.tmux_activation import _wait_for_client

        proc = SimpleNamespace(
            returncode=0,
            communicate=AsyncMock(return_value=(b"4242\n9001\n", b"")),
            kill=MagicMock(),
            wait=AsyncMock(return_value=0),
        )
        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
        ) as create_proc:
            assert await _wait_for_client(server._tmux_mgr_default, "demo", 9001) is True

        assert create_proc.await_args is not None
        args = create_proc.await_args.args
        assert "list-clients" in args
        assert "#{client_pid}" in args
        assert args[args.index("-t") + 1] == "=demo:"

    @pytest.mark.asyncio
    async def test_gives_up_when_our_pid_never_appears(self, server: WebSocketServer) -> None:
        from gobby.servers.websocket.tmux_activation import _wait_for_client

        proc = SimpleNamespace(
            returncode=0,
            communicate=AsyncMock(return_value=(b"4242\n", b"")),
            kill=MagicMock(),
            wait=AsyncMock(return_value=0),
        )
        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
        ) as create_proc:
            assert (
                await _wait_for_client(server._tmux_mgr_default, "demo", 9001, timeout=0.05)
                is False
            )

        # It kept polling to the deadline rather than giving up on one miss.
        assert create_proc.await_count >= 2
        assert proc.communicate.await_count == create_proc.await_count

    @pytest.mark.asyncio
    async def test_a_failing_list_clients_never_reports_registration(
        self, server: WebSocketServer
    ) -> None:
        from gobby.servers.websocket.tmux_activation import _wait_for_client

        proc = SimpleNamespace(
            returncode=1,
            communicate=AsyncMock(return_value=(b"9001\n", b"no server running")),
            kill=MagicMock(),
            wait=AsyncMock(return_value=1),
        )
        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
        ) as create_proc:
            assert (
                await _wait_for_client(server._tmux_mgr_default, "demo", 9001, timeout=0.05)
                is False
            )

        # A nonzero list-clients is never read as "registered", even though the
        # matching pid is present on stdout.
        assert create_proc.await_count >= 1
        assert proc.kill.call_count == 0
