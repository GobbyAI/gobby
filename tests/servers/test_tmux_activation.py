"""Tests for the tmux-client attach path of the WebSocket ``TmuxMixin``.

A tmux row is viewed through a real ``tmux attach-session`` client running in
a daemon PTY sized to the browser. ``terminal_attach`` only reserves; the
first ``terminal_resize`` carries the real geometry and builds the client.
"""

from __future__ import annotations

import contextlib
import json
import signal
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.tmux.alt_screen import AltScreenFilter
from gobby.agents.tmux.history import HistoryCapture, HistoryCaptureError
from gobby.agents.tmux.pty_bridge import BridgeInfo, TmuxPTYBridge
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig
from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.terminals import Terminal
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.unit

# The tty the registration poll reports, and therefore the one the capture's
# command list is expected to repaint.
CLIENT_TTY = "/dev/ttys009"


class MockWebSocket:
    def __init__(self) -> None:
        self.sent_messages: list[str] = []
        self.closed = False
        self.remote_address = ("127.0.0.1", 12345)

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)

    def messages_of_type(self, msg_type: str) -> list[dict[str, Any]]:
        decoded = (json.loads(raw) for raw in self.sent_messages)
        return [m for m in decoded if m.get("type") == msg_type]


class TracingWebSocket(MockWebSocket):
    """Records each frame type into a shared call trace."""

    def __init__(self, calls: list[str]) -> None:
        super().__init__()
        self._calls = calls

    async def send(self, message: str) -> None:
        await super().send(message)
        self._calls.append(f"send:{json.loads(message)['type']}")


@pytest.fixture
def server() -> WebSocketServer:
    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    return WebSocketServer(config, MagicMock(), AsyncMock(return_value="test-user"))


@pytest.fixture
def row(server: WebSocketServer) -> Terminal:
    terminal = make_memory_terminal(session_name="demo")
    server.terminal_manager = MemoryTerminalStore(terminal)
    return terminal


def make_bridge(
    *,
    terminal_id: str,
    session_name: str = "demo",
    rows: int = 40,
    cols: int = 120,
    returncode: int | None = None,
) -> BridgeInfo:
    """A ``BridgeInfo`` whose client process is a controllable stand-in."""
    proc = SimpleNamespace(pid=9001, returncode=returncode, send_signal=MagicMock())
    return BridgeInfo(
        master_fd=42,
        proc=cast(Any, proc),
        session_name=session_name,
        socket_name="",
        rows=rows,
        cols=cols,
        terminal_id=terminal_id,
        config=TmuxConfig(socket_name="", socket_path="/private/tmp/tmux-501/default"),
    )


@dataclass
class Harness:
    """Every boundary the activation state machine reaches, mocked."""

    attach: AsyncMock
    get_bridge: AsyncMock
    list_bridges: AsyncMock
    detach: AsyncMock
    reader: MagicMock
    capture: AsyncMock
    set_option: AsyncMock
    refresh_client: AsyncMock
    has_session: AsyncMock
    bridge: BridgeInfo
    calls: list[str]


@contextlib.contextmanager
def activation_harness(
    server: WebSocketServer,
    *,
    bridge: BridgeInfo,
    bridges: dict[str, BridgeInfo] | None = None,
    calls: list[str] | None = None,
) -> Iterator[Harness]:
    if calls is None:
        calls = []
    reader = MagicMock()
    reader.stop_reader = AsyncMock(return_value=True)

    async def record_attach(**_kwargs: Any) -> int:
        calls.append("attach")
        return 42

    async def record_capture(*_args: Any, **_kwargs: Any) -> HistoryCapture:
        calls.append("capture")
        return HistoryCapture(
            text="hist", truncated=False, dropped_bytes=0, total_bytes=4, repainted=True
        )

    async def record_start_reader(*_args: Any, **_kwargs: Any) -> bool:
        calls.append("start_reader")
        return True

    async def record_refresh(*_args: Any, **_kwargs: Any) -> None:
        calls.append("refresh_client")

    async def record_list_bridges() -> dict[str, BridgeInfo]:
        calls.append("list_bridges")
        return dict(bridges or {})

    reader.start_reader = AsyncMock(side_effect=record_start_reader)

    with contextlib.ExitStack() as stack:
        harness = Harness(
            attach=stack.enter_context(
                patch.object(
                    server._tmux_bridge, "attach", new_callable=AsyncMock, side_effect=record_attach
                )
            ),
            get_bridge=stack.enter_context(
                patch.object(
                    server._tmux_bridge, "get_bridge", new_callable=AsyncMock, return_value=bridge
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
            capture=stack.enter_context(
                patch(
                    "gobby.servers.websocket.tmux_activation.capture_history",
                    new_callable=AsyncMock,
                    side_effect=record_capture,
                )
            ),
            set_option=stack.enter_context(
                patch.object(TmuxSessionManager, "set_option", new_callable=AsyncMock)
            ),
            refresh_client=stack.enter_context(
                patch.object(
                    TmuxSessionManager,
                    "refresh_client",
                    new_callable=AsyncMock,
                    side_effect=record_refresh,
                )
            ),
            has_session=stack.enter_context(
                patch.object(
                    TmuxSessionManager, "has_session", new_callable=AsyncMock, return_value=True
                )
            ),
            bridge=bridge,
            calls=calls,
        )
        stack.enter_context(
            patch(
                "gobby.servers.websocket.tmux_activation._wait_for_client",
                new_callable=AsyncMock,
                return_value=CLIENT_TTY,
            )
        )
        stack.enter_context(
            patch("gobby.agents.pty_reader.get_pty_reader_manager", return_value=reader)
        )
        yield harness


async def reserve(server: WebSocketServer, websocket: MockWebSocket, row: Terminal) -> str:
    """Run the attach half and return the reserved attachment id."""
    await server._handle_terminal_attach(
        websocket, {"terminal_id": row.id, "frame_delivery": "proxy", "request_id": "r1"}
    )
    result = websocket.messages_of_type("terminal_attach_result")[-1]
    assert result["success"] is True
    return str(result["attachment_id"])


async def resize(
    server: WebSocketServer,
    websocket: MockWebSocket,
    attachment_id: str,
    rows: int = 40,
    cols: int = 120,
) -> None:
    await server._handle_terminal_resize(
        websocket, {"attachment_id": attachment_id, "rows": rows, "cols": cols}
    )


class TestTmuxAttachReservation:
    async def test_attach_reserves_a_tmux_client_and_takes_the_lease(
        self, server: WebSocketServer, row: Terminal
    ) -> None:
        ws = MockWebSocket()

        with activation_harness(server, bridge=make_bridge(terminal_id=row.id)) as harness:
            attachment_id = await reserve(server, ws, row)

        result = ws.messages_of_type("terminal_attach_result")[0]
        assert result["backend"] == "tmux"
        assert result["terminal_id"] == row.id
        # Nothing is built until the browser says how big it is.
        harness.attach.assert_not_awaited()
        pending = server._tmux_pending[attachment_id]
        assert pending.session_name == "demo"
        assert row.locator is not None
        assert pending.config.socket_path == row.locator["socket_path"]
        assert pending.owner is ws
        # A tmux client is a typing seat, so the viewer holds the lease.
        assert server.lease_registry.holder(row.id) == attachment_id

    async def test_a_second_viewer_displaces_the_first_lease(
        self, server: WebSocketServer, row: Terminal
    ) -> None:
        first = MockWebSocket()
        second = MockWebSocket()
        server.clients[first] = {}
        server.clients[second] = {}

        with activation_harness(server, bridge=make_bridge(terminal_id=row.id)):
            first_id = await reserve(server, first, row)
            second_id = await reserve(server, second, row)

        assert server.lease_registry.holder(row.id) == second_id
        lost = first.messages_of_type("terminal_lease_lost")
        assert lost and lost[0]["attachment_id"] == first_id
        assert lost[0]["holder"] == second_id
        # The first viewer's reservation is a second live socket's and stays.
        assert first_id in server._tmux_pending
        assert second_id in server._tmux_pending

    async def test_native_rows_never_reserve_a_tmux_client(self, server: WebSocketServer) -> None:
        native = make_memory_terminal(backend="native", session_name="native-demo")
        server.terminal_manager = MemoryTerminalStore(native)
        ws = MockWebSocket()

        with activation_harness(server, bridge=make_bridge(terminal_id=native.id)) as harness:
            await server._handle_terminal_attach(
                ws,
                {
                    "terminal_id": native.id,
                    "frame_delivery": "direct",
                    "request_id": "r1",
                },
            )

        result = ws.messages_of_type("terminal_attach_result")[0]
        assert result["success"] is True
        assert result["backend"] == "native"
        assert server._tmux_pending == {}
        harness.attach.assert_not_awaited()

    async def test_native_rows_under_proxy_delivery_never_reserve_a_tmux_client(
        self, server: WebSocketServer
    ) -> None:
        native = make_memory_terminal(backend="native", session_name="native-demo")
        server.terminal_manager = MemoryTerminalStore(native)
        ws = MockWebSocket()

        with activation_harness(server, bridge=make_bridge(terminal_id=native.id)) as harness:
            await server._handle_terminal_attach(
                ws,
                {
                    "terminal_id": native.id,
                    "frame_delivery": "proxy",
                    "request_id": "r1",
                },
            )

        # No runtime is registered here, so the attach honestly fails —
        # and still must not touch the tmux reservation path.
        result = ws.messages_of_type("terminal_attach_result")[0]
        assert result["success"] is False
        assert result["code"] == "runtime_unavailable"
        assert server._tmux_pending == {}
        harness.attach.assert_not_awaited()


class TestTmuxActivation:
    async def test_first_resize_activates_in_order(
        self, server: WebSocketServer, row: Terminal
    ) -> None:
        calls: list[str] = []
        ws = TracingWebSocket(calls)

        with activation_harness(
            server, bridge=make_bridge(terminal_id=row.id), calls=calls
        ) as harness:
            attachment_id = await reserve(server, ws, row)
            await resize(server, ws, attachment_id, rows=40, cols=120)

        assert calls == [
            "send:terminal_attach_result",
            "list_bridges",
            "attach",
            "capture",
            "send:terminal_attach_history",
            "start_reader",
        ]
        # The repaint is issued inside the capture's own tmux command list, so
        # a captured attachment never makes a second refresh call of its own.
        harness.refresh_client.assert_not_awaited()
        assert harness.capture.await_args is not None
        assert harness.capture.await_args.kwargs["refresh_tty"] == CLIENT_TTY
        assert harness.capture.await_args.kwargs["max_lines"] == TmuxConfig().attach_history_lines
        assert harness.attach.await_args is not None
        attach_kwargs = harness.attach.await_args.kwargs
        assert (attach_kwargs["rows"], attach_kwargs["cols"]) == (40, 120)
        assert attach_kwargs["session_name"] == "demo"
        assert attach_kwargs["terminal_id"] == row.id
        assert attach_kwargs["streaming_id"] == attachment_id
        assert row.locator is not None
        assert attach_kwargs["config"].socket_path == row.locator["socket_path"]
        harness.set_option.assert_any_await("demo", "status", "off")
        harness.set_option.assert_any_await("demo", "mouse", "on")

        history = ws.messages_of_type("terminal_attach_history")[0]
        assert history["terminal_id"] == row.id
        assert history["attachment_id"] == attachment_id
        assert history["text"] == "hist"
        assert history["truncated"] is False
        assert history["unavailable"] is False
        assert history["total_bytes"] == 4

        # The reader streams under the attachment id, which is the id the web
        # matches terminal_output frames against.
        assert harness.reader.start_reader.await_args is not None
        agent = harness.reader.start_reader.await_args.args[0]
        assert (agent.run_id, agent.master_fd) == (attachment_id, 42)
        transform = harness.reader.start_reader.await_args.kwargs["transform"]
        assert isinstance(transform, AltScreenFilter)
        assert transform("\x1b[?1049hrepaint") == "repaint"

        assert attachment_id not in server._tmux_pending
        assert attachment_id in server._tmux_client_bridges[ws]

    async def test_capture_failure_degrades_and_still_streams(
        self, server: WebSocketServer, row: Terminal
    ) -> None:
        ws = MockWebSocket()

        with activation_harness(server, bridge=make_bridge(terminal_id=row.id)) as harness:
            harness.capture.side_effect = HistoryCaptureError("capture-pane timed out")
            attachment_id = await reserve(server, ws, row)
            await resize(server, ws, attachment_id)

        history = ws.messages_of_type("terminal_attach_history")[0]
        assert history["unavailable"] is True
        assert history["text"] == ""
        assert harness.reader.start_reader.await_count == 1
        # No capture also means no command-list repaint, so the screen paint
        # falls back to an explicit refresh.
        harness.refresh_client.assert_awaited_once()
        assert attachment_id not in server._tmux_pending

    async def test_a_session_gone_during_attach_finalizes_the_attachment(
        self, server: WebSocketServer, row: Terminal
    ) -> None:
        ws = MockWebSocket()

        with activation_harness(server, bridge=make_bridge(terminal_id=row.id)) as harness:
            harness.capture.side_effect = HistoryCaptureError("no session")
            harness.has_session.return_value = False
            attachment_id = await reserve(server, ws, row)
            await resize(server, ws, attachment_id)

        finalized = ws.messages_of_type("terminal_attachment_finalized")
        assert len(finalized) == 1
        assert finalized[0]["reason"] == "session_missing"
        assert finalized[0]["terminal_id"] == row.id
        assert finalized[0]["attachment_id"] == attachment_id
        harness.detach.assert_awaited_with(attachment_id)
        harness.reader.start_reader.assert_not_awaited()
        assert ws.messages_of_type("terminal_attach_history") == []
        assert server.lease_registry.get(attachment_id) is None
        assert attachment_id not in server._tmux_pending
        assert attachment_id not in server._tmux_client_bridges.get(ws, set())

    async def test_resize_from_a_foreign_socket_builds_nothing(
        self, server: WebSocketServer, row: Terminal
    ) -> None:
        owner = MockWebSocket()
        intruder = MockWebSocket()

        with activation_harness(server, bridge=make_bridge(terminal_id=row.id)) as harness:
            attachment_id = await reserve(server, owner, row)
            await resize(server, intruder, attachment_id)

        harness.attach.assert_not_awaited()
        assert attachment_id in server._tmux_pending

    async def test_a_later_resize_goes_to_the_bridge_and_repaints_only_on_change(
        self, server: WebSocketServer, row: Terminal
    ) -> None:
        ws = MockWebSocket()
        bridge = make_bridge(terminal_id=row.id)

        with activation_harness(server, bridge=bridge) as harness:
            attachment_id = await reserve(server, ws, row)
            await resize(server, ws, attachment_id)
            with patch.object(
                server._tmux_bridge, "resize", new_callable=AsyncMock, return_value=None
            ) as unchanged:
                await resize(server, ws, attachment_id)
            unchanged.assert_awaited_once_with(attachment_id, 40, 120)
            harness.refresh_client.assert_not_awaited()
            with patch.object(
                server._tmux_bridge, "resize", new_callable=AsyncMock, return_value=bridge
            ) as changed:
                await resize(server, ws, attachment_id, rows=41, cols=121)
            changed.assert_awaited_once_with(attachment_id, 41, 121)

        harness.refresh_client.assert_awaited_once_with("demo")
        harness.attach.assert_awaited_once()
        assert len(ws.messages_of_type("terminal_attach_history")) == 1

    async def test_reattach_reaps_this_clients_stale_bridge_only(
        self, server: WebSocketServer, row: Terminal
    ) -> None:
        ws = MockWebSocket()
        stale = make_bridge(terminal_id=row.id)
        foreign = make_bridge(terminal_id="another-terminal", session_name="other")
        server._tmux_client_bridges[ws] = {"stale", "foreign"}

        with activation_harness(
            server,
            bridge=make_bridge(terminal_id=row.id),
            bridges={"stale": stale, "foreign": foreign},
        ) as harness:
            attachment_id = await reserve(server, ws, row)
            await resize(server, ws, attachment_id)

        harness.detach.assert_awaited_once_with("stale")
        harness.reader.stop_reader.assert_awaited_once_with("stale")
        assert server._tmux_client_bridges[ws] == {"foreign", attachment_id}


class TestTmuxBridgeInput:
    async def test_input_writes_raw_bytes_to_the_client_pty(
        self, server: WebSocketServer, row: Terminal
    ) -> None:
        ws = MockWebSocket()

        with activation_harness(server, bridge=make_bridge(terminal_id=row.id)):
            attachment_id = await reserve(server, ws, row)
            await resize(server, ws, attachment_id)
            with (
                patch.object(
                    server._tmux_bridge, "get_master_fd", new_callable=AsyncMock, return_value=42
                ),
                patch("gobby.servers.websocket.tmux.os.write") as write,
            ):
                await server._handle_terminal_input(
                    ws,
                    {
                        "terminal_id": row.id,
                        "attachment_id": attachment_id,
                        "data": "\x03",
                        "client_write_seq": 1,
                    },
                )

        write.assert_called_once_with(42, b"\x03")
        outcome = ws.messages_of_type("terminal_write_outcome")[0]
        assert outcome["outcome"] == "delivered"
        assert outcome["client_write_seq"] == 1


class TestTmuxBridgeTeardown:
    async def test_detach_tears_down_the_bridge(
        self, server: WebSocketServer, row: Terminal
    ) -> None:
        ws = MockWebSocket()

        with activation_harness(server, bridge=make_bridge(terminal_id=row.id)) as harness:
            attachment_id = await reserve(server, ws, row)
            await resize(server, ws, attachment_id)
            await server._handle_terminal_detach(
                ws, {"terminal_id": row.id, "attachment_id": attachment_id}
            )

        harness.reader.stop_reader.assert_awaited_once_with(attachment_id)
        harness.detach.assert_awaited_once_with(attachment_id)
        assert attachment_id not in server._tmux_client_bridges.get(ws, set())
        assert server.lease_registry.get(attachment_id) is None
        assert ws.messages_of_type("terminal_detach_result")[0]["success"] is True

    async def test_a_closing_client_tears_down_its_bridges(
        self, server: WebSocketServer, row: Terminal
    ) -> None:
        ws = MockWebSocket()

        with activation_harness(server, bridge=make_bridge(terminal_id=row.id)) as harness:
            attachment_id = await reserve(server, ws, row)
            await resize(server, ws, attachment_id)
            await server._cleanup_tmux_client(ws)

        harness.detach.assert_awaited_once_with(attachment_id)
        assert ws not in server._tmux_client_bridges
        assert server.lease_registry.get(attachment_id) is None

    async def test_a_closing_client_drops_its_reservation(
        self, server: WebSocketServer, row: Terminal
    ) -> None:
        ws = MockWebSocket()

        with activation_harness(server, bridge=make_bridge(terminal_id=row.id)) as harness:
            attachment_id = await reserve(server, ws, row)
            await server._cleanup_tmux_client(ws)
            await resize(server, ws, attachment_id)

        harness.attach.assert_not_awaited()
        assert attachment_id not in server._tmux_pending


class TestBridgeResizeGuard:
    async def test_a_resize_to_the_current_size_repaints_nothing(self) -> None:
        """#20805: the geometry tmux already runs at is not a resize."""
        bridge = TmuxPTYBridge()
        info = make_bridge(terminal_id="t1", rows=40, cols=120)
        bridge._bridges["a"] = info

        with patch("gobby.agents.tmux.pty_bridge.fcntl.ioctl") as ioctl:
            assert await bridge.resize("a", 40, 120) is None
            ioctl.assert_not_called()

            assert await bridge.resize("a", 41, 120) is info
            ioctl.assert_called_once()

        cast(Any, info.proc).send_signal.assert_called_once_with(signal.SIGWINCH)
        recorded = await bridge.get_bridge("a")
        assert recorded is not None
        assert (recorded.rows, recorded.cols) == (41, 120)
        # Only what tmux was told is remembered, so the same size is now a no-op.
        with patch("gobby.agents.tmux.pty_bridge.fcntl.ioctl") as ioctl:
            assert await bridge.resize("a", 41, 120) is None
            ioctl.assert_not_called()
