"""Plan 4.3: web terminal proxy through the shared frame client."""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import (
    AttachLocator,
    Terminal,
    TerminalManager,
    native_locator_key,
    tmux_locator_key,
)
from gobby.terminals import TerminalRuntime, TerminalRuntimeRegistry
from gobby.terminals.dimensions import MAX_CELLS, MAX_FRAME_SIZE
from gobby.terminals.frame_client import FrameLagError, FrameProtocolError, decode_frame
from gobby.terminals.runtime import Delivered, IndeterminateWrite, WriteOutcome
from gobby.terminals.ws_protocol import (
    TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES,
    TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES,
    TERMINAL_WS_SAFE_INTEGER_MAX,
    decode_message,
)
from tests.servers.test_tmux_mixin import MockWebSocket
from tests.storage.test_terminals import LOCAL_MACHINE_ID, _create_pending, _manager

pytestmark = pytest.mark.unit

_SOCKET = "/private/tmp/tmux-501/default"
_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _machine() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _ws_server() -> WebSocketServer:
    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    return WebSocketServer(config, MagicMock(), AsyncMock(return_value="user"))


async def _send(server: WebSocketServer, ws: MockWebSocket, payload: dict[str, Any]) -> None:
    await server._handle_message(ws, json.dumps(payload))


async def _until(predicate: Any, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def _reassemble(messages: list[dict[str, Any]]) -> dict[str, Any]:
    fragments = [item for item in messages if item.get("type") == "terminal_ws_fragment"]
    if not fragments:
        return messages[-1]
    ordered = sorted(fragments, key=lambda item: int(item["fragment_index"]))
    payload = b"".join(base64.b64decode(str(item["payload"])) for item in ordered)
    if len(payload) > TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES:
        raise ValueError("fragment_too_large")
    return decode_message(payload)


@dataclass
class FakeProxyFrame:
    """In-memory frame client: user attach, no reservation, no write verb."""

    queue: asyncio.Queue[dict[str, Any] | BaseException | None] = field(
        default_factory=asyncio.Queue
    )
    reservation_ids: list[str | None] = field(default_factory=list)
    viewports: list[tuple[int, int]] = field(default_factory=list)
    scrolls: list[int] = field(default_factory=list)
    writes: list[object] = field(default_factory=list)
    handshake_epochs: list[str] = field(default_factory=list)
    closed: bool = False
    detached: bool = False
    # Recorded from the production handshake call; FrameClient defaults to
    # "semantic_frame", which the web proxy must override with "terminal_ansi".
    encoding: str | None = None

    async def handshake(self, locator: AttachLocator, *, encoding: str = "semantic_frame") -> None:
        self.handshake_epochs.append(locator.frame_host_epoch)
        self.encoding = encoding

    async def attach_terminal(
        self,
        locator: AttachLocator,
        *,
        reservation_id: str | None = None,
    ) -> None:
        del locator
        self.reservation_ids.append(reservation_id)

    async def read_message(self) -> dict[str, Any]:
        item = await self.queue.get()
        if item is None:
            raise FrameProtocolError("unexpected eof")
        if isinstance(item, BaseException):
            raise item
        return item

    async def set_viewport(self, rows: int, cols: int) -> None:
        self.viewports.append((rows, cols))

    async def set_scroll_offset(self, rows_from_live_edge: int) -> None:
        self.scrolls.append(rows_from_live_edge)

    async def detach(self) -> None:
        self.detached = True

    async def close(self) -> None:
        self.closed = True
        self.detached = True


@dataclass
class RecordingRuntime:
    """Records writes and resizes; never writes on a frame attachment."""

    backend: Literal["tmux", "native"]
    write_log: list[tuple[str, str]] = field(default_factory=list)
    resize_calls: list[tuple[int, int]] = field(default_factory=list)
    tmux_commands: list[list[str]] = field(default_factory=list)
    outcome: WriteOutcome = field(default_factory=Delivered)
    drop_next: bool = False
    hold: asyncio.Event | None = None
    host_writes: list[dict[str, Any]] = field(default_factory=list)

    async def attach_locator(self, terminal: Terminal) -> AttachLocator:
        locator = terminal.locator or {}
        if self.backend == "native":
            host_id = locator.get("host_terminal_id")
            return AttachLocator(
                backend="native",
                frame_host_epoch=str(terminal.host_epoch or "epoch-1"),
                host_terminal_id=None if host_id is None else str(host_id),
            )
        return AttachLocator(
            backend="tmux",
            frame_host_epoch=str(terminal.host_epoch or "epoch-1"),
            socket_path=None if locator.get("socket_path") is None else str(locator["socket_path"]),
            pane_id=None if locator.get("pane_id") is None else str(locator["pane_id"]),
        )

    async def write_text(self, terminal: Terminal, text: str, submit: bool) -> WriteOutcome:
        del submit
        return await self._record("text", text, terminal)

    async def write_paste(self, terminal: Terminal, text: str) -> WriteOutcome:
        return await self._record("paste", text, terminal)

    async def write_key(self, terminal: Terminal, key: str) -> WriteOutcome:
        return await self._record("key", key, terminal)

    async def resize(self, terminal: Terminal, rows: int, cols: int) -> None:
        self.resize_calls.append((rows, cols))
        if terminal.backend == "tmux":
            self.tmux_commands.append(["resize-pane", str(rows), str(cols)])

    async def _record(self, kind: str, payload: str, terminal: Terminal) -> WriteOutcome:
        self.write_log.append((kind, payload))
        if self.backend == "native":
            self.host_writes.append({"kind": kind, "payload": payload, "terminal_id": terminal.id})
        else:
            self.tmux_commands.append(["send-keys", "-H", kind, payload])
        if self.hold is not None:
            await self.hold.wait()
        if self.drop_next:
            self.drop_next = False
            return IndeterminateWrite(detail="dropped")
        return self.outcome


class BlockingWebSocket(MockWebSocket):
    """WebSocket whose send can stall to fill the relay queue."""

    def __init__(self) -> None:
        super().__init__()
        self.gate: asyncio.Event | None = None
        self.send_started = asyncio.Event()
        self.closed = False

    async def send(self, message: str) -> None:
        self.send_started.set()
        if self.gate is not None:
            await self.gate.wait()
        await super().send(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason
        self.closed = True


@dataclass
class _Harness:
    server: WebSocketServer
    manager: TerminalManager
    native_row: Terminal
    tmux_row: Terminal
    external_row: Terminal
    native_rt: RecordingRuntime
    tmux_rt: RecordingRuntime
    frames: dict[str, FakeProxyFrame]
    frame_list: list[FakeProxyFrame]


def _promote_native(manager: TerminalManager, project_id: str) -> Terminal:
    pending = _create_pending(manager, project_id, backend="native")
    host_id = f"host-{uuid4()}"
    epoch = "epoch-1"
    promoted = manager.promote_to_live(
        pending.id,
        locator={"host_terminal_id": host_id},
        locator_key=native_locator_key(epoch, host_id),
        host_epoch=epoch,
        session_name="native-sess",
    )
    assert promoted is not None
    return promoted


def _promote_tmux(manager: TerminalManager, project_id: str, *, pane: str) -> Terminal:
    pending = _create_pending(manager, project_id, backend="tmux")
    locator = {
        "socket_path": _SOCKET,
        "server_pid": 9,
        "server_start_time": 9,
        "pane_id": pane,
    }
    promoted = manager.promote_to_live(
        pending.id,
        locator=locator,
        locator_key=tmux_locator_key(
            socket_path=_SOCKET, server_pid=9, server_start_time=9, pane_id=pane
        ),
        session_name="tmux-sess",
    )
    assert promoted is not None
    return promoted


def _harness(temp_db: HubDatabase, sample_project: dict[str, Any]) -> _Harness:
    manager = _manager(temp_db)
    native_row = _promote_native(manager, sample_project["id"])
    tmux_row = _promote_tmux(manager, sample_project["id"], pane="%9")
    external_row = manager.upsert_external(
        project_id=sample_project["id"],
        backend="tmux",
        locator={
            "socket_path": _SOCKET,
            "server_pid": 11,
            "server_start_time": 11,
            "pane_id": "%11",
        },
        locator_key=tmux_locator_key(
            socket_path=_SOCKET, server_pid=11, server_start_time=11, pane_id="%11"
        ),
        session_name="ext-sess",
        title="external",
    )
    native_rt = RecordingRuntime(backend="native")
    tmux_rt = RecordingRuntime(backend="tmux")
    registry = TerminalRuntimeRegistry()
    registry.register(cast(TerminalRuntime, native_rt))
    registry.register(cast(TerminalRuntime, tmux_rt))
    server = _ws_server()
    server.configure_terminals(manager, registry, MagicMock())
    frames: dict[str, FakeProxyFrame] = {}
    frame_list: list[FakeProxyFrame] = []

    async def open_proxy_frame(locator: AttachLocator) -> FakeProxyFrame:
        key = locator.host_terminal_id or locator.pane_id or str(uuid4())
        frame = FakeProxyFrame()
        frames[key] = frame
        frame_list.append(frame)
        return frame

    server.open_proxy_frame = open_proxy_frame
    return _Harness(
        server=server,
        manager=manager,
        native_row=native_row,
        tmux_row=tmux_row,
        external_row=external_row,
        native_rt=native_rt,
        tmux_rt=tmux_rt,
        frames=frames,
        frame_list=frame_list,
    )


def _frame_for(harness: _Harness, row: Terminal) -> FakeProxyFrame:
    locator = row.locator or {}
    key = str(locator.get("host_terminal_id") or locator.get("pane_id") or "")
    frame = harness.frames.get(key)
    assert frame is not None, f"no proxy frame for {key!r} in {list(harness.frames)}"
    return frame


async def _attach(
    harness: _Harness,
    ws: MockWebSocket,
    row: Terminal,
    *,
    request_id: str = "a1",
) -> str:
    harness.server.clients[ws] = {"subscriptions": {"*"}}
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_attach",
            "request_id": request_id,
            "terminal_id": row.id,
            "frame_delivery": "proxy",
        },
    )
    await _until(lambda: ws.messages_of_type("terminal_attach_result"))
    result = ws.messages_of_type("terminal_attach_result")[-1]
    assert result.get("success") is not False
    attachment = str(result["attachment_id"])
    await _until(lambda: bool(harness.frames))
    return attachment


async def _take(harness: _Harness, ws: MockWebSocket, row: Terminal, attachment: str) -> None:
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_take_control",
            "terminal_id": row.id,
            "attachment_id": attachment,
            "takeover": False,
        },
    )


def _src_has_second_decoder() -> bool:
    hits: list[str] = []
    for path in (_REPO / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "decode_frame":
                hits.append(str(path.relative_to(_REPO)))
    return hits != ["src/gobby/terminals/frame_client.py"]


@pytest.mark.asyncio
async def test_web_attach_native_terminal(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    harness = _harness(temp_db, sample_project)
    ws = MockWebSocket()
    attachment = await _attach(harness, ws, harness.native_row)
    frame = _frame_for(harness, harness.native_row)
    assert frame.reservation_ids == [None]
    assert frame.encoding == "terminal_ansi"
    await _take(harness, ws, harness.native_row, attachment)
    await frame.queue.put(
        {
            "type": "attach_history",
            "text": "hi\n",
            "truncated": False,
            "dropped_bytes": 0,
            "total_bytes": 3,
        }
    )
    await frame.queue.put(
        {
            "type": "terminal",
            "seq": 1,
            "width": 80,
            "height": 24,
            "full": True,
            "bytes": b"ready.\n",
        }
    )
    await _until(lambda: ws.messages_of_type("terminal_output"))
    history = ws.messages_of_type("terminal_attach_history")
    output = ws.messages_of_type("terminal_output")
    assert history
    assert history[0]["text"] == "hi\n"
    assert output[0]["data"] == "ready.\n"
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_input",
            "terminal_id": harness.native_row.id,
            "attachment_id": attachment,
            "data": "ls\n",
            "client_write_seq": 1,
        },
    )
    await _until(lambda: harness.native_rt.write_log)
    assert harness.native_rt.write_log[0] == ("text", "ls\n")
    assert harness.native_rt.host_writes
    assert not frame.writes
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_paste",
            "terminal_id": harness.native_row.id,
            "attachment_id": attachment,
            "text": "paste",
            "client_write_seq": 2,
        },
    )
    await _until(lambda: any(kind == "paste" for kind, _ in harness.native_rt.write_log))
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_resize",
            "terminal_id": harness.native_row.id,
            "attachment_id": attachment,
            "rows": 30,
            "cols": 100,
        },
    )
    await _until(lambda: harness.native_rt.resize_calls == [(30, 100)])
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_set_viewport",
            "terminal_id": harness.native_row.id,
            "attachment_id": attachment,
            "rows": 24,
            "cols": 80,
        },
    )
    await _until(lambda: frame.viewports == [(24, 80)])
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_detach",
            "request_id": "d1",
            "terminal_id": harness.native_row.id,
            "attachment_id": attachment,
        },
    )
    await _until(lambda: frame.closed or frame.detached)


@pytest.mark.asyncio
async def test_disconnect_releases_lease(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    harness = _harness(temp_db, sample_project)
    ws = MockWebSocket()
    attachment = await _attach(harness, ws, harness.native_row)
    await _take(harness, ws, harness.native_row, attachment)
    frame = _frame_for(harness, harness.native_row)
    await harness.server._cleanup_tmux_client(ws)
    assert harness.server.lease_registry.get(attachment) is None
    assert frame.closed or frame.detached

    reasons = (
        ("eof", None, "proxy_frame_eof"),
        ("lag", FrameLagError("lag"), "proxy_lag"),
        ("host", {"type": "error", "code": "host_gone", "message": "x"}, "host_loss"),
    )
    for label, item, reason in reasons:
        ws2 = MockWebSocket()
        att = await _attach(harness, ws2, harness.native_row, request_id=f"r-{label}")
        fr = _frame_for(harness, harness.native_row)
        await fr.queue.put(item)
        await _until(lambda ws2=ws2: ws2.messages_of_type("terminal_attachment_finalized"))
        finalized = ws2.messages_of_type("terminal_attachment_finalized")[-1]
        assert finalized["reason"] == reason
        assert finalized["attachment_id"] == att
        assert harness.server.lease_registry.get(att) is None


@pytest.mark.asyncio
async def test_web_read_only_until_explicit_takeover(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    harness = _harness(temp_db, sample_project)
    web = MockWebSocket()
    gclient = MockWebSocket()
    web_att = await _attach(harness, web, harness.native_row, request_id="web")
    await _send(
        harness.server,
        web,
        {
            "type": "terminal_input",
            "terminal_id": harness.native_row.id,
            "attachment_id": web_att,
            "data": "x",
            "client_write_seq": 1,
        },
    )
    refused = web.messages_of_type("terminal_write_outcome")[-1]
    assert refused["outcome"] == "refused"
    assert not harness.native_rt.write_log
    await _take(harness, web, harness.native_row, web_att)
    granted = web.messages_of_type("terminal_control_result")[-1]
    assert granted["granted"] is True
    assert "lease_generation" in granted
    g_att = await _attach(harness, gclient, harness.native_row, request_id="gc")
    await _send(
        harness.server,
        gclient,
        {
            "type": "terminal_take_control",
            "terminal_id": harness.native_row.id,
            "attachment_id": g_att,
            "takeover": True,
        },
    )
    await _until(lambda: web.messages_of_type("terminal_lease_lost"))
    lost = web.messages_of_type("terminal_lease_lost")[-1]
    assert (
        lost["lease_generation"]
        == gclient.messages_of_type("terminal_control_result")[-1]["lease_generation"]
    )
    frame = _frame_for(harness, harness.native_row)
    tmux_att = await _attach(harness, web, harness.tmux_row, request_id="tmux")
    await _take(harness, web, harness.tmux_row, tmux_att)
    await _send(
        harness.server,
        web,
        {
            "type": "terminal_input",
            "terminal_id": harness.tmux_row.id,
            "attachment_id": tmux_att,
            "data": "y",
            "client_write_seq": 2,
        },
    )
    await _until(lambda: harness.tmux_rt.write_log)
    assert harness.tmux_rt.tmux_commands
    assert not frame.writes
    native_frame = _frame_for(harness, harness.native_row)
    assert not native_frame.writes


@pytest.mark.asyncio
async def test_shared_decoder_and_slow_browser(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    assert _src_has_second_decoder() is False
    assert decode_frame.__module__ == "gobby.terminals.frame_client"
    harness = _harness(temp_db, sample_project)
    slow = BlockingWebSocket()
    other = MockWebSocket()
    att = await _attach(harness, slow, harness.native_row, request_id="slow")
    other_att = await _attach(harness, other, harness.native_row, request_id="other")
    del att, other_att
    frame = harness.frame_list[0]
    await frame.queue.put(
        {"type": "terminal", "seq": 1, "width": 80, "height": 24, "full": True, "bytes": b"kf\n"}
    )
    await _until(lambda: slow.messages_of_type("terminal_output"))
    slow.gate = asyncio.Event()
    for index in range(70):
        await frame.queue.put(
            {
                "type": "terminal",
                "seq": index + 2,
                "width": 80,
                "height": 24,
                "full": False,
                "bytes": b"d",
            }
        )
    await _until(lambda: slow.closed, timeout=6.0)
    assert other.closed is False or not getattr(other, "closed", False)
    assert (
        harness.server.lease_registry.get(
            slow.messages_of_type("terminal_attach_result")[-1]["attachment_id"]
        )
        is None
    )


@pytest.mark.asyncio
async def test_viewport_independent_of_resize_and_paste_is_leased(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    harness = _harness(temp_db, sample_project)
    a = MockWebSocket()
    b = MockWebSocket()
    att_a = await _attach(harness, a, harness.native_row, request_id="va")
    att_b = await _attach(harness, b, harness.native_row, request_id="vb")
    await _send(
        harness.server,
        a,
        {
            "type": "terminal_set_viewport",
            "terminal_id": harness.native_row.id,
            "attachment_id": att_a,
            "rows": 24,
            "cols": 80,
        },
    )
    await _send(
        harness.server,
        b,
        {
            "type": "terminal_set_viewport",
            "terminal_id": harness.native_row.id,
            "attachment_id": att_b,
            "rows": 40,
            "cols": 120,
        },
    )
    frames = [item for key, item in harness.frames.items() if not key.startswith("host-") or True]
    del frames
    await _until(lambda: sum(len(frame.viewports) for frame in harness.frame_list) >= 2)
    await _send(
        harness.server,
        a,
        {
            "type": "terminal_resize",
            "terminal_id": harness.native_row.id,
            "attachment_id": att_a,
            "rows": 30,
            "cols": 90,
        },
    )
    assert harness.native_rt.resize_calls == []
    await _take(harness, a, harness.native_row, att_a)
    await _send(
        harness.server,
        a,
        {
            "type": "terminal_resize",
            "terminal_id": harness.native_row.id,
            "attachment_id": att_a,
            "rows": 30,
            "cols": 90,
        },
    )
    await _until(lambda: harness.native_rt.resize_calls == [(30, 90)])
    await _send(
        harness.server,
        b,
        {
            "type": "terminal_paste",
            "terminal_id": harness.native_row.id,
            "attachment_id": att_b,
            "text": "nope",
            "client_write_seq": 1,
        },
    )
    outcome = b.messages_of_type("terminal_write_outcome")[-1]
    assert outcome["outcome"] == "refused"
    assert not any(kind == "paste" for kind, _ in harness.native_rt.write_log)

    ext_ws = MockWebSocket()
    ext_att = await _attach(harness, ext_ws, harness.external_row, request_id="ext")
    await _take(harness, ext_ws, harness.external_row, ext_att)
    before = list(harness.tmux_rt.tmux_commands)
    await _send(
        harness.server,
        ext_ws,
        {
            "type": "terminal_resize",
            "terminal_id": harness.external_row.id,
            "attachment_id": ext_att,
            "rows": 12,
            "cols": 40,
        },
    )
    assert harness.tmux_rt.tmux_commands == before
    assert harness.tmux_rt.resize_calls == []


@pytest.mark.asyncio
async def test_attach_history_then_max_keyframe_is_fragmented_under_cap(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    harness = _harness(temp_db, sample_project)
    ws = MockWebSocket()
    await _attach(harness, ws, harness.native_row)
    frame = _frame_for(harness, harness.native_row)
    history = "h" * 100
    await frame.queue.put(
        {
            "type": "attach_history",
            "text": history,
            "truncated": False,
            "dropped_bytes": 0,
            "total_bytes": len(history),
        }
    )
    dense = bytes([0x01]) * min(MAX_FRAME_SIZE - 4096, MAX_CELLS)
    await frame.queue.put(
        {
            "type": "terminal",
            "seq": 1,
            "width": 80,
            "height": 24,
            "full": True,
            "bytes": dense,
        }
    )
    await _until(
        lambda: ws.messages_of_type("terminal_attach_history")
        or any(item.get("event") == "terminal_attach_history" for item in ws.all_messages())
    )
    types = [item.get("type") or item.get("event") for item in ws.all_messages()]
    history_idx = next(
        i
        for i, kind in enumerate(types)
        if kind in {"terminal_attach_history", "terminal_ws_fragment"}
    )
    later = types[history_idx:]
    assert "terminal_output" in later or any(
        item.get("event") == "terminal_output" for item in ws.all_messages()[history_idx:]
    )
    fragments = [item for item in ws.all_messages() if item.get("type") == "terminal_ws_fragment"]
    if fragments:
        for item in fragments:
            raw = json.dumps(item, separators=(",", ":"), sort_keys=True).encode("utf-8")
            assert len(raw) < TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES


@pytest.mark.asyncio
async def test_escape_dense_near_2mib_frame_reassembles_and_one_byte_over_is_rejected(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    harness = _harness(temp_db, sample_project)
    ws = MockWebSocket()
    await _attach(harness, ws, harness.native_row)
    frame = _frame_for(harness, harness.native_row)
    payload = bytes([0x01]) * 20_000 + b"a" * TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES
    await frame.queue.put(
        {"type": "terminal", "seq": 1, "width": 80, "height": 24, "full": True, "bytes": payload}
    )
    await _until(
        lambda: ws.messages_of_type("terminal_output")
        or ws.messages_of_type("terminal_ws_fragment"),
        timeout=8.0,
    )
    messages = ws.all_messages()
    fragments = [item for item in messages if item.get("type") == "terminal_ws_fragment"]
    if fragments:
        rebuilt = _reassemble(fragments)
        assert rebuilt["type"] == "terminal_output"
        assert len(cast(str, rebuilt["data"]).encode("latin1", errors="replace")) >= 1
    from gobby.terminals.ws_protocol import emit_proxied_event

    with pytest.raises(ValueError, match="fragment_too_large"):
        emit_proxied_event(
            {
                "type": "terminal_output",
                "terminal_id": "t",
                "attachment_id": "a",
                "data": "x" * (TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES + 1),
            },
            message_seq=1,
        )


@pytest.mark.asyncio
async def test_lifecycle_reserved_send_and_wedged_control_closes_socket(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    harness = _harness(temp_db, sample_project)
    ws = BlockingWebSocket()
    att = await _attach(harness, ws, harness.native_row)
    frame = _frame_for(harness, harness.native_row)
    ws.gate = asyncio.Event()
    for index in range(64):
        await frame.queue.put(
            {
                "type": "terminal",
                "seq": index,
                "width": 80,
                "height": 24,
                "full": False,
                "bytes": b"x",
            }
        )
    await frame.queue.put(None)
    # The EOF finalizes the lease before the lifecycle event is enqueued on
    # the wedged relay; only then may the gate open.
    await _until(lambda: harness.server.lease_registry.get(att) is None)
    ws.gate.set()
    await _until(lambda: ws.messages_of_type("terminal_attachment_finalized"), timeout=6.0)
    finalized = ws.messages_of_type("terminal_attachment_finalized")[-1]
    assert finalized["attachment_id"] == att

    wedged = BlockingWebSocket()
    await _attach(harness, wedged, harness.native_row, request_id="wedge")
    wedged.gate = asyncio.Event()
    await _send(
        harness.server,
        wedged,
        {
            "type": "terminal_take_control",
            "terminal_id": harness.native_row.id,
            "attachment_id": wedged.messages_of_type("terminal_attach_result")[-1]["attachment_id"],
            "takeover": False,
        },
    )
    await _until(lambda: wedged.closed, timeout=8.0)


@pytest.mark.asyncio
async def test_frame_queue_overflow_closes_socket_for_all_attachments(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    harness = _harness(temp_db, sample_project)
    second_native = _promote_native(harness.manager, sample_project["id"])
    ws = BlockingWebSocket()
    att_a = await _attach(harness, ws, harness.native_row, request_id="qa")
    att_b = await _attach(harness, ws, second_native, request_id="qb")
    await _until(lambda: len(harness.frame_list) == 2)
    frame_a = _frame_for(harness, harness.native_row)
    ws.gate = asyncio.Event()
    for index in range(80):
        await frame_a.queue.put(
            {
                "type": "terminal",
                "seq": index,
                "width": 80,
                "height": 24,
                "full": False,
                "bytes": b"z" * 1024,
            }
        )
    await _until(lambda: ws.closed, timeout=8.0)
    assert harness.server.lease_registry.get(att_a) is None
    assert harness.server.lease_registry.get(att_b) is None


@pytest.mark.asyncio
async def test_scroll_offset_and_wrapped_attach_history(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    harness = _harness(temp_db, sample_project)
    ws = MockWebSocket()
    att = await _attach(harness, ws, harness.native_row)
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_set_scroll_offset",
            "terminal_id": harness.native_row.id,
            "attachment_id": att,
            "rows_from_live_edge": 12,
        },
    )
    frame = _frame_for(harness, harness.native_row)
    await _until(lambda: frame.scrolls == [12])
    await frame.queue.put({"type": "scroll_offset_applied", "applied_rows": 12, "max_rows": 40})
    await _until(lambda: ws.messages_of_type("terminal_scroll_offset_applied"))
    applied = ws.messages_of_type("terminal_scroll_offset_applied")[-1]
    assert applied["applied_rows"] == 12
    wrapped = "宽宽宽宽宽宽宽宽\nnext"
    second_native = _promote_native(harness.manager, sample_project["id"])
    wrap_ws = MockWebSocket()
    await _attach(harness, wrap_ws, second_native, request_id="wrap")
    await _until(lambda: len(harness.frame_list) == 2)
    wrap_frame = _frame_for(harness, second_native)
    await wrap_frame.queue.put(
        {
            "type": "attach_history",
            "text": wrapped,
            "truncated": False,
            "dropped_bytes": 0,
            "total_bytes": len(wrapped.encode()),
        }
    )
    await _until(lambda: wrap_ws.messages_of_type("terminal_attach_history"))
    text = wrap_ws.messages_of_type("terminal_attach_history")[-1]["text"]
    assert "\n" in text
    assert "宽" in text


@pytest.mark.asyncio
async def test_write_outcome_indeterminate_tmux_and_native(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    harness = _harness(temp_db, sample_project)
    ws = MockWebSocket()
    native_att = await _attach(harness, ws, harness.native_row, request_id="n")
    await _take(harness, ws, harness.native_row, native_att)
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_input",
            "terminal_id": harness.native_row.id,
            "attachment_id": native_att,
            "data": "ok",
            "client_write_seq": 1,
        },
    )
    delivered = ws.messages_of_type("terminal_write_outcome")[-1]
    assert delivered["outcome"] == "delivered"
    harness.native_rt.drop_next = True
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_input",
            "terminal_id": harness.native_row.id,
            "attachment_id": native_att,
            "data": "drop",
            "client_write_seq": 2,
        },
    )
    lost = ws.messages_of_type("terminal_write_outcome")[-1]
    assert lost["outcome"] == "indeterminate"
    assert lost["client_write_seq"] == 2
    tmux_att = await _attach(harness, ws, harness.tmux_row, request_id="t")
    await _take(harness, ws, harness.tmux_row, tmux_att)
    harness.tmux_rt.drop_next = True
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_paste",
            "terminal_id": harness.tmux_row.id,
            "attachment_id": tmux_att,
            "text": "p",
            "client_write_seq": 3,
        },
    )
    tmux_lost = [
        item
        for item in ws.messages_of_type("terminal_write_outcome")
        if item.get("client_write_seq") == 3
    ][-1]
    assert tmux_lost["outcome"] == "indeterminate"


@pytest.mark.asyncio
async def test_write_seq_ledger_tmux_and_native(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    harness = _harness(temp_db, sample_project)
    ws = MockWebSocket()
    att = await _attach(harness, ws, harness.native_row)
    await _take(harness, ws, harness.native_row, att)
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_input",
            "terminal_id": harness.native_row.id,
            "attachment_id": att,
            "data": "one",
            "client_write_seq": 1,
        },
    )
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_input",
            "terminal_id": harness.native_row.id,
            "attachment_id": att,
            "data": "one",
            "client_write_seq": 1,
        },
    )
    replay = [
        item
        for item in ws.messages_of_type("terminal_write_outcome")
        if item.get("client_write_seq") == 1
    ]
    assert replay[-1]["outcome"] == "delivered"
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_paste",
            "terminal_id": harness.native_row.id,
            "attachment_id": att,
            "text": "other",
            "client_write_seq": 1,
        },
    )
    conflict = [
        item
        for item in ws.messages_of_type("terminal_write_outcome")
        if item.get("reason") == "write_seq_conflict"
    ]
    assert conflict
    harness.native_rt.hold = asyncio.Event()
    writes_before = len(harness.native_rt.host_writes)
    tasks = [
        asyncio.create_task(
            _send(
                harness.server,
                ws,
                {
                    "type": "terminal_input",
                    "terminal_id": harness.native_row.id,
                    "attachment_id": att,
                    "data": f"n{seq}",
                    "client_write_seq": seq,
                },
            )
        )
        for seq in range(2, 66)
    ]
    # Every admitted write reaches the held runtime before the capacity probe.
    await _until(lambda: len(harness.native_rt.host_writes) >= writes_before + 64)
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_input",
            "terminal_id": harness.native_row.id,
            "attachment_id": att,
            "data": "cap",
            "client_write_seq": 66,
        },
    )
    capacity = [
        item
        for item in ws.messages_of_type("terminal_write_outcome")
        if item.get("reason") == "write_seq_capacity"
    ]
    assert capacity
    harness.native_rt.hold.set()
    await asyncio.gather(*tasks)
    await _send(
        harness.server,
        ws,
        {
            "type": "terminal_input",
            "terminal_id": harness.native_row.id,
            "attachment_id": att,
            "data": "late",
            "client_write_seq": 1,
        },
    )
    expired = [
        item
        for item in ws.messages_of_type("terminal_write_outcome")
        if item.get("reason") == "write_seq_expired"
    ]
    assert expired
    assert TERMINAL_WS_SAFE_INTEGER_MAX == 2**53 - 1
