"""Native WS publication from Python to gdaemon (#24620, B1)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gc
import json
import tracemalloc
import warnings
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.websockets import WebSocket

import gobby.runner_backend as runner_backend
import gobby.servers.websocket.native_publish as native_publish
import gobby.terminals.ws_protocol as ws_protocol
from gobby.config.bootstrap import BootstrapConfig
from gobby.servers.websocket.broadcast import BroadcastMixin
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.native_bridge import NativeBridgeAdapter, add_server_subscriptions
from gobby.servers.websocket.native_publish import (
    NativePublisher,
    native_publisher_from_environment,
)
from gobby.servers.websocket.proxy_relay import ProxyHub
from gobby.servers.websocket.server import WebSocketServer
from gobby.servers.websocket.session_control import SessionControlMixin
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.ws_protocol import (
    TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES,
    canonical_json,
    fragment_event,
)
from gobby.utils.json_helpers import json_dumps
from tests.fixtures.native_ws_probe_records import RECORDS_FILE, recording_bind

pytestmark = pytest.mark.unit

SECRET = "native-publish-test-front-door"
TOKEN = "backend-token-1"
BASE_URL = "http://127.0.0.1:60887"
GEN1 = f"{1:032x}"
GEN2 = f"{2:032x}"
CONFIG = WebSocketConfig(max_message_size=4096, ping_interval=7, ping_timeout=3)
# gdaemon's PUBLISH_LIMIT: a publish body over this is refused with 413.
DAEMON_PUBLISH_LIMIT = 16 * 1024 * 1024
WORKSPACE_ROUTING_FIELDS = ("type", "kind", "workspace_id", "project_id", "seq", "timestamp")


@pytest.mark.parametrize("stage", ["bind", "rebind"])
@pytest.mark.parametrize("error", [TimeoutError(), httpx.ReadTimeout("front door stalled")])
async def test_bind_failure_logs_exception_type_and_recovers(
    stage: str,
    error: Exception,
    supervisor_token: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exceptions with empty str() retain a useful diagnostic during recovery."""
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.01)
    attempts = 0
    failed_attempt = 1 if stage == "bind" else 2

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == native_publish.BIND_PATH:
            attempts += 1
            if attempts == failed_attempt:
                raise error
            return httpx.Response(200, json={"generation": GEN1 if attempts == 1 else GEN2})
        return httpx.Response(204)

    publisher = NativePublisher(
        secret=SECRET,
        base_url=BASE_URL,
        config=CONFIG,
        transport=httpx.MockTransport(handle),
    )
    try:
        await publisher.start()
        if stage == "rebind":
            publisher.fail_generation(GEN1)
        await _until(lambda: publisher.generation == GEN2)
        await asyncio.wait_for(publisher.publish({"type": "agent_event"}), 1.0)
        messages = [record.getMessage() for record in caplog.records]
        diagnostic = next(message for message in messages if f"Native WS {stage} failed" in message)
        assert type(error).__name__ in diagnostic
        if str(error):
            assert str(error) in diagnostic
    finally:
        await publisher.stop()


async def test_publish_requests_stay_within_a_deadline_sized_batch(supervisor_token: str) -> None:
    """A POST's body must finish inside REQUEST_TIMEOUT however much queued during a stall."""
    publish_bodies: list[int] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == native_publish.BIND_PATH:
            return httpx.Response(200, json={"generation": GEN1})
        publish_bodies.append(len(request.content))
        return httpx.Response(204)

    publisher = NativePublisher(
        secret=SECRET,
        base_url=BASE_URL,
        config=CONFIG,
        transport=httpx.MockTransport(handle),
    )
    frame = {"type": "terminal_output", "data": "x" * (1 << 20)}
    try:
        await publisher.start()
        pending = [
            publisher.publish(
                {"type": "terminal_output"}, [frame], source_bytes=_source_bytes([frame])
            )
            for _ in range(4)
        ]
        await asyncio.wait_for(asyncio.gather(*pending), 5.0)
    finally:
        await publisher.stop()
    assert len(publish_bodies) > 1
    assert max(publish_bodies) <= 2 * (1 << 20)


async def test_large_publish_splits_into_bounded_ops_in_frame_order(supervisor_token: str) -> None:
    """One publish call of many frames never becomes one oversized request body."""
    daemon = _Daemon()
    frames = [{"type": "terminal_output", "seq": seq, "data": "x" * (1 << 20)} for seq in range(10)]
    publisher = _publisher(daemon)
    try:
        await publisher.start()
        await asyncio.wait_for(
            publisher.publish(
                {"type": "terminal_output"}, frames, source_bytes=_source_bytes(frames)
            ),
            5.0,
        )
    finally:
        await publisher.stop()
    delivered = [json.loads(payload)["seq"] for op in daemon.ops(GEN1) for payload in op["frames"]]
    assert delivered == list(range(10))
    assert len(daemon.body_sizes) > 1
    assert max(daemon.body_sizes) <= 4 * (1 << 20) + 1024


async def test_publish_frame_larger_than_the_op_cap_goes_alone(supervisor_token: str) -> None:
    """A frame too large to share an op with its neighbours is sent alone, in frame order."""
    daemon = _Daemon()
    frames = [
        {"type": "terminal_output", "seq": 0, "data": "x" * (1 << 20)},
        {"type": "terminal_output", "seq": 1, "data": "x" * (5 << 20)},
        {"type": "terminal_output", "seq": 2, "data": "x" * (1 << 20)},
    ]
    publisher = _publisher(daemon)
    try:
        await publisher.start()
        await asyncio.wait_for(
            publisher.publish(
                {"type": "terminal_output"}, frames, source_bytes=_source_bytes(frames)
            ),
            5.0,
        )
    finally:
        await publisher.stop()
    delivered = [json.loads(payload)["seq"] for op in daemon.ops(GEN1) for payload in op["frames"]]
    assert delivered == [0, 1, 2]
    assert [len(op["frames"]) for op in daemon.ops(GEN1)] == [1, 1, 1]


def test_publish_op_bytes_match_the_generic_encoder() -> None:
    """An op assembled from frame literals encodes exactly as json_dumps encodes the op."""
    message = {"type": "terminal_output"}
    payloads = [
        json_dumps({"seq": 1, "data": 'quote " and \\ slash'}),
        json_dumps({"seq": 2, "data": "héllo"}),
    ]
    literals = [json_dumps(payload).encode() for payload in payloads]
    op = {"op": "publish", "filter": message, "frames": payloads}
    assert native_publish._publish_op(json_dumps(message), literals) == json_dumps(op).encode()


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "terminal_ws_fragment", "payload": "QUJD" * 50_000},
        {"payload": "x" * (native_publish._LITERAL_SLICE_CHARS - 1)},
        {"payload": "x" * native_publish._LITERAL_SLICE_CHARS},
        {"payload": "x" * (native_publish._LITERAL_SLICE_CHARS + 1)},
        {"payload": "é" * 20_000 + "😀" * 9_000},
        {"text": 'quote " and \\ slash\n\t\x00' * 3_000},
        {"when": datetime(2026, 11, 5, 12, 30, tzinfo=UTC), "day": date(2026, 11, 5)},
        {"pair": ("a", None), "items": [{"b": 1, "a": [True, None, 1.5, float("nan")]}]},
        {"empty": {}, "list": [], "text": ""},
    ],
    ids=[
        "multi-slice-payload",
        "slice-minus-one",
        "exact-slice",
        "slice-plus-one",
        "non-bmp-and-accents",
        "quotes-backslashes-controls",
        "datetime-and-date",
        "tuple-and-nested-list",
        "empty-containers",
    ],
)
async def test_frame_literal_bytes_match_the_generic_encoder(frame: dict[str, Any]) -> None:
    assert await native_publish._frame_literal(frame) == json_dumps(json_dumps(frame)).encode()


async def test_frame_literal_rejects_an_unencodable_value() -> None:
    with pytest.raises(TypeError):
        await native_publish._frame_literal({"bad": {1, 2}})


async def test_frame_literal_yields_while_it_encodes_a_large_frame() -> None:
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    try:
        literal = await native_publish._frame_literal({"payload": "A" * (1024 * 1024)})
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert len(literal) > 1024 * 1024
    assert ticks >= 8


def test_fragment_event_encodes_a_bounded_multiple_of_its_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fragmenting a cap-sized event encodes a bounded multiple of its bytes.

    The count comes from canonical_json calls, so the bound holds at any host load.
    """
    empty = len(canonical_json(_workspace_event("")))
    raw = canonical_json(
        _workspace_event("x" * (TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES - empty))
    )
    encoded: list[int] = []
    real = ws_protocol.canonical_json

    def counting(message: Any) -> bytes:
        data = real(message)
        encoded.append(len(data))
        return data

    monkeypatch.setattr(ws_protocol, "canonical_json", counting)
    frames = fragment_event(
        event="workspace_event",
        terminal_id="ws-1",
        attachment_id="ws-1",
        message_seq=42,
        complete_json=raw,
    )
    assert len(frames) > 2
    assert sum(encoded) <= 4 * len(raw)


def test_fragment_event_base64_encodes_each_input_byte_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each event byte reaches the base64 encoder exactly once.

    The encoder input total is fixed by the event, so the assertion holds at any host load.
    """
    empty = len(canonical_json(_workspace_event("")))
    raw = canonical_json(
        _workspace_event("x" * (TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES - empty))
    )
    encoded: list[int] = []
    real_b64encode = base64.b64encode

    def counting(data: bytes) -> bytes:
        encoded.append(len(data))
        return real_b64encode(data)

    monkeypatch.setattr(base64, "b64encode", counting)
    frames = fragment_event(
        event="workspace_event",
        terminal_id="ws-1",
        attachment_id="ws-1",
        message_seq=42,
        complete_json=raw,
    )
    assert len(frames) > 2
    assert sum(encoded) == len(raw)
    assert b"".join(base64.b64decode(frame["payload"]) for frame in frames) == raw


class _Host(BroadcastMixin):
    """A broadcaster whose only sink is the native publisher."""

    def __init__(self, publisher: NativePublisher) -> None:
        self.clients: dict[Any, dict[str, Any]] = {}
        self.native_publisher = publisher


def _workspace_event(scrollback: str) -> dict[str, Any]:
    """A workspace_event shaped as broadcast_workspace_event builds it, rows included."""
    return {
        "type": "workspace_event",
        "kind": "pane.added",
        "workspace_id": "ws-1",
        "project_id": "proj-1",
        "seq": 42,
        "timestamp": "2026-11-02T00:00:00+00:00",
        "workspace": None,
        "tabs": [{"id": "tab-1", "project_id": "proj-1"}],
        "panes": [{"id": "pane-1", "tab_id": "tab-1", "scrollback": scrollback}],
    }


async def test_workspace_event_at_the_fragment_cap_publishes_bounded_ops(
    supervisor_token: str,
) -> None:
    """A fragmented workspace event at the cap stays under the daemon bound.

    Every op's filter holds the routing fields alone. The rows travel only in the
    fragments, which arrive in order and reassemble to the event's own bytes.
    """
    empty = len(canonical_json(_workspace_event("")))
    message = _workspace_event("x" * (TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES - empty))
    raw = canonical_json(message)
    assert len(raw) == TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES
    daemon = _Daemon()
    daemon.publish_limit = DAEMON_PUBLISH_LIMIT
    publisher = _publisher(daemon)
    host = _Host(publisher)
    try:
        await publisher.start()
        await asyncio.wait_for(host._publish_workspace_event(message), 5.0)
    finally:
        await publisher.stop()

    assert max(daemon.body_sizes) <= DAEMON_PUBLISH_LIMIT
    assert len(daemon.binds) == 1
    ops = daemon.ops(GEN1)
    route = {field: message[field] for field in WORKSPACE_ROUTING_FIELDS}
    assert [op["filter"] for op in ops] == [route] * len(ops)
    fragments = [json.loads(payload) for op in ops for payload in op["frames"]]
    assert len(fragments) > 2
    assert [fragment["fragment_index"] for fragment in fragments] == list(range(len(fragments)))
    assert b"".join(base64.b64decode(fragment["payload"]) for fragment in fragments) == raw


async def test_publish_preparation_peak_stays_within_queued_bytes_and_op_transients(
    supervisor_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preparing a cap-sized publish holds its peak allocation near the queued wire bytes.

    Encoding runs in a task after publish() returns, so the peak is read where the call
    exchanges its ops. The exchange runs synchronously once preparation finishes and before
    any POST starts, so the window covers the whole encode and none of the daemon side.
    The queued ops are the only whole-wire copies that outlive the call. Each op is
    assembled from at most PUBLISH_CHUNK bytes, so the transient allocation above them
    is a few op sizes. Traced allocation is fixed by the event, so the bound holds at
    any host load.
    """
    empty = len(canonical_json(_workspace_event("")))
    message = _workspace_event("x" * (TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES - empty))
    frames = fragment_event(
        event="workspace_event",
        terminal_id="ws-1",
        attachment_id="ws-1",
        message_seq=42,
        complete_json=canonical_json(message),
    )
    route = {field: message[field] for field in WORKSPACE_ROUTING_FIELDS}
    source_bytes = _source_bytes(frames)
    daemon = _Daemon()
    daemon.publish_limit = DAEMON_PUBLISH_LIMIT
    publisher = _publisher(daemon)
    enqueue = publisher._enqueue
    exchanged: list[tuple[int, int]] = []

    def measure_exchange(
        turn: asyncio.Future[None], generation: str | None, encoded: list[bytes]
    ) -> list[asyncio.Future[None]]:
        futures = enqueue(turn, generation, encoded)
        _, peak = tracemalloc.get_traced_memory()
        exchanged.append((peak, sum(len(op) for op in encoded)))
        return futures

    try:
        await publisher.start()
        monkeypatch.setattr(publisher, "_enqueue", measure_exchange)
        tracemalloc.start()
        try:
            base, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            settled = publisher.publish(route, frames, source_bytes=source_bytes)
            await asyncio.wait_for(settled, 5.0)
        finally:
            tracemalloc.stop()
    finally:
        await publisher.stop()

    [(peak, queued)] = exchanged
    assert peak - base <= queued + 4 * native_publish.PUBLISH_CHUNK


class _Daemon:
    """gdaemon's private bind and publish endpoints, with a gate to stall the sender."""

    def __init__(self, log: list[str] | None = None) -> None:
        self.generations = [f"{n:032x}" for n in range(1, 10)]
        self.bind_status: list[int] = []
        self.binds: list[httpx.Request] = []
        self.bind_times: list[float] = []
        self.requests: list[dict[str, Any]] = []
        self.body_sizes: list[int] = []
        self.publish_limit: int | None = None
        self.publish_headers: list[httpx.Headers] = []
        self.received = asyncio.Event()
        self.gate: asyncio.Event | None = None
        self.mode = "ok"
        self.in_flight = 0
        self.max_in_flight = 0
        self.log = log if log is not None else []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_gobby/ws/bind":
            self.binds.append(request)
            self.bind_times.append(asyncio.get_running_loop().time())
            status = self.bind_status.pop(0) if self.bind_status else 200
            if status != 200:
                return httpx.Response(status)
            return httpx.Response(200, json={"generation": self.generations.pop(0)})
        if request.url.path != "/_gobby/ws/publish":
            return httpx.Response(404)
        index = len(self.requests)
        self.requests.append(json.loads(request.content))
        self.body_sizes.append(len(request.content))
        if self.publish_limit is not None and len(request.content) > self.publish_limit:
            return httpx.Response(413)
        self.publish_headers.append(request.headers)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.received.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.mode == "failed":
                return httpx.Response(503)
            if self.mode == "transport":
                raise httpx.ConnectError("connection refused", request=request)
            if self.mode == "timed_out":
                raise httpx.ReadTimeout("timed out", request=request)
            if self.mode == "blackholed":
                await asyncio.Event().wait()
            self.log.append(f"ack:{index}")
            return httpx.Response(204)
        finally:
            self.in_flight -= 1

    def ops(self, generation: str | None = None) -> list[dict[str, Any]]:
        return [
            op
            for request in self.requests
            if generation is None or request["generation"] == generation
            for op in request["ops"]
        ]


class _Leg:
    def __init__(self, *, hang: bool = False) -> None:
        self.hang = hang
        self.closed: list[int] = []
        self.cancelled = False

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append(code)
        if self.hang:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise


class _Socket:
    """The Starlette side of a bridge leg, recording what the adapter sends."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)
        self.log.append("reply")

    async def send_bytes(self, data: bytes) -> None:
        raise AssertionError("no binary reply expected")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.log.append(f"close:{code}")


class _RelaySocket:
    """A terminal relay's client socket: sends stall until its leg is retired."""

    def __init__(self, hub: ProxyHub, sending: asyncio.Event) -> None:
        self.hub = hub
        self.sending = sending
        self.closed: list[int] = []

    async def send(self, _raw: str) -> None:
        self.sending.set()
        await asyncio.Event().wait()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append(code)
        # A closed leg ends its client handler, which drops the socket's relays as ws_close.
        await self.hub.drop_socket(self, "ws_close")


class _PythonOnlySocket:
    """A Python-only client; hashable like a real connection, since attach waiters form a set."""

    def __init__(self) -> None:
        self.subscriptions: set[str] = set()
        self.send = AsyncMock()


@pytest.fixture
def supervisor_token() -> Iterator[str]:
    runner_backend._token.reset()
    runner_backend._token.feed(f"{TOKEN}\n".encode())
    yield TOKEN
    runner_backend._token.reset()


def _publisher(daemon: _Daemon) -> NativePublisher:
    return NativePublisher(
        secret=SECRET,
        base_url=BASE_URL,
        config=CONFIG,
        transport=httpx.MockTransport(daemon.handle),
    )


def _running_server(
    publisher: NativePublisher, monkeypatch: pytest.MonkeyPatch
) -> tuple[WebSocketServer, AsyncMock]:
    server = WebSocketServer(CONFIG, MagicMock(), AsyncMock(return_value="u"))
    server.native_publisher = publisher
    shutdown = AsyncMock()
    server.lease_registry = MagicMock()
    server.lease_registry.shutdown_lifecycle_publication = shutdown
    monkeypatch.setattr(server, "_cleanup_idle_sessions", AsyncMock())
    return server, shutdown


async def _until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def _turns(count: int = 50) -> None:
    """Run the loop for a fixed number of turns; MockTransport only progresses on turns."""
    for _ in range(count):
        await asyncio.sleep(0)


def _count_token_polls(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    polls: list[str | None] = []

    def counted() -> str | None:
        token = runner_backend.supervisor_write_token()
        polls.append(token)
        return token

    monkeypatch.setattr(native_publish, "supervisor_write_token", counted)
    return polls


def _publish_op(
    message: dict[str, Any], frames: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    payloads = [json_dumps(frame) for frame in frames or []] or [json_dumps(message)]
    return {"op": "publish", "filter": json.loads(json_dumps(message)), "frames": payloads}


def _source_bytes(frames: list[dict[str, Any]]) -> int:
    """The bytes the frames encode from, which the caller passes as their source size."""
    return len(json_dumps(frames))


def _adapter(publisher: NativePublisher, socket: _Socket, leg: int = 7) -> NativeBridgeAdapter:
    return NativeBridgeAdapter(
        cast(WebSocket, socket),
        user_id="local-web-0000abcd",
        leg=leg,
        generation=GEN1,
        publisher=publisher,
        remote_address=("127.0.0.1", 50001),
    )


async def test_publisher_binds_and_publishes_encoded_frames(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await asyncio.wait_for(publisher.start(), timeout=2.0)
    try:
        assert publisher.generation == GEN1
        [bind] = daemon.binds
        assert bind.url == httpx.URL(f"{BASE_URL}/_gobby/ws/bind")
        assert bind.headers["X-Gobby-Front-Door"] == SECRET
        assert bind.headers["X-Gobby-Backend-Token"] == supervisor_token
        assert json.loads(bind.content) == {
            "max_message_size": 4096,
            "ping_interval": 7,
            "ping_timeout": 3,
        }

        # No client is connected and no leg is registered: every broadcast still publishes.
        daemon.gate = asyncio.Event()
        first = {"type": "session_message", "session_id": "s-1", "text": "héllo ☃"}
        second = {"type": "terminal_output", "run_id": "r-1"}
        fragments = [
            {"type": "terminal_output", "seq": 1, "at": datetime(2026, 10, 16, tzinfo=UTC)},
            {"type": "terminal_output", "seq": 2, "data": "\u001b[0m"},
        ]
        events = ["workspace_event:workspace_id=w-1", "agent_event"]
        sent_first = publisher.publish(first)
        await _until(daemon.received.is_set)
        later = [
            publisher.publish(second, frames=fragments, source_bytes=_source_bytes(fragments)),
            publisher.subscribe(7, events),
            publisher.publish(first),
        ]
        await _turns()
        assert len(daemon.requests) == 1
        assert not sent_first.done()
        assert not any(future.done() for future in later)
        daemon.gate.set()
        assert await asyncio.wait_for(asyncio.gather(sent_first, *later), 1.0) == [None] * 4

        assert daemon.max_in_flight == 1
        assert daemon.requests == [
            {"generation": GEN1, "ops": [_publish_op(first)]},
            {
                "generation": GEN1,
                "ops": [
                    _publish_op(second, fragments),
                    {"op": "subscribe", "leg": 7, "events": events},
                    _publish_op(first),
                ],
            },
        ]
        for headers in daemon.publish_headers:
            assert headers["X-Gobby-Front-Door"] == SECRET
            assert headers["X-Gobby-Backend-Token"] == supervisor_token
        assert (publisher.retained_bytes, publisher.retained_ops) == (0, 0)

        # The request bound covers the whole body, wrapper and separators included: two
        # operations that fit the bound by their own bytes go in two requests.
        assert native_publish.MAX_RETAINED_BYTES == 128 * 1024 * 1024
        op_size = len(json_dumps(_publish_op(second)).encode())
        monkeypatch.setattr(native_publish, "MAX_RETAINED_BYTES", 2 * op_size)
        sent = len(daemon.requests)
        pair = [publisher.publish(second), publisher.publish(second)]
        assert await asyncio.wait_for(asyncio.gather(*pair), 1.0) == [None, None]
        assert daemon.requests[sent:] == [{"generation": GEN1, "ops": [_publish_op(second)]}] * 2
        assert max(daemon.body_sizes[sent:]) <= 2 * op_size
        assert publisher.generation == GEN1

        # One operation whose request would exceed the bound fails its generation unsent.
        monkeypatch.setattr(native_publish, "MAX_RETAINED_BYTES", op_size)
        oversized = publisher.publish(second)
        await asyncio.wait_for(oversized, 1.0)
        await _turns()
        assert len(daemon.requests) == sent + 2
        assert publisher.generation != GEN1
    finally:
        await publisher.stop()


@pytest.mark.parametrize("mode", ["timed_out", "failed", "blackholed", "transport"])
async def test_ambiguous_publish_retires_generation_without_retry(
    mode: str, supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        native_publish.REQUEST_TIMEOUT,
        native_publish.REBIND_INTERVAL,
        native_publish.LEG_CLOSE_TIMEOUT,
    ) == (15.0, 5.0, 1.0)
    monkeypatch.setattr(native_publish, "REQUEST_TIMEOUT", 0.2)
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.3)
    monkeypatch.setattr(native_publish, "LEG_CLOSE_TIMEOUT", 0.2)
    loop = asyncio.get_running_loop()
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        leg, hung = _Leg(), _Leg(hang=True)
        assert publisher.register_leg(GEN1, leg)
        assert publisher.register_leg(GEN1, hung)
        daemon.gate = asyncio.Event()
        daemon.mode = mode
        daemon.bind_status = [503]
        batch = publisher.publish({"type": "agent_event", "n": 1})
        await _until(daemon.received.is_set)
        queued = publisher.publish({"type": "agent_event", "n": 2})
        daemon.gate.set()
        assert list(await asyncio.wait_for(asyncio.gather(batch, queued), 1.0)) == [None, None]
        failed_at = loop.time()

        await _until(lambda: leg.closed == [1011] and hung.cancelled, timeout=0.5)
        assert hung.closed == [1011]
        await _until(lambda: publisher.generation == GEN2, timeout=2.0)
        first_rebind, second_rebind = daemon.bind_times[1:]
        assert first_rebind - failed_at < 0.3
        assert second_rebind - first_rebind >= 0.3
        assert [request["generation"] for request in daemon.requests] == [GEN1]
        assert daemon.ops() == [_publish_op({"type": "agent_event", "n": 1})]
        assert (publisher.retained_bytes, publisher.retained_ops) == (0, 0)

        daemon.mode = "ok"
        daemon.gate = None
        newer = _Leg()
        assert not publisher.register_leg(GEN1, newer)
        assert publisher.register_leg(GEN2, newer)
        publisher.fail_generation(GEN1)
        assert publisher.generation == GEN2
        after = {"type": "agent_event", "n": 3}
        await asyncio.wait_for(publisher.publish(after), 1.0)
        assert daemon.ops(GEN2) == [_publish_op(after)]
        assert newer.closed == []
    finally:
        await publisher.stop()


async def test_stalled_publish_retires_relay_legs_without_fencing_lifecycle(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out publish retires the legs its terminal relays ride; lifecycle keeps publishing."""
    monkeypatch.setattr(native_publish, "REQUEST_TIMEOUT", 0.2)
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.3)
    monkeypatch.setattr(native_publish, "LEG_CLOSE_TIMEOUT", 0.2)
    daemon = _Daemon()
    publisher = _publisher(daemon)
    registry = TerminalLeaseRegistry()
    hub = ProxyHub(SimpleNamespace(_leases=lambda: registry))
    sending = asyncio.Event()
    socket = _RelaySocket(hub, sending)
    await publisher.start()
    try:
        assert publisher.register_leg(GEN1, socket)
        exited = asyncio.create_task(
            hub.emit_lifecycle(socket, {"type": "terminal_event", "event": "exited"})
        )
        await asyncio.wait_for(sending.wait(), 1.0)
        daemon.gate = asyncio.Event()
        stalled = publisher.publish({"type": "agent_event", "n": 1})
        await _until(lambda: 1011 in socket.closed, timeout=2.0)
        await asyncio.wait_for(exited, 1.0)
        await asyncio.wait_for(stalled, 1.0)

        daemon.gate = None
        await _until(lambda: publisher.generation == GEN2, timeout=2.0)
        later = {"type": "agent_event", "n": 2}
        await asyncio.wait_for(publisher.publish(later), 1.0)
        assert daemon.ops(GEN2) == [_publish_op(later)]

        async def record(_stamped: dict[str, Any]) -> None:
            return None

        pending = {"type": "terminal_event", "event": "pending"}
        stamped = await asyncio.wait_for(registry.publish_lifecycle(pending, record), 1.0)
        assert stamped["event"] == "pending"
    finally:
        await publisher.stop()


async def test_publish_timeout_logs_batch_size_and_elapsed_time(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A timed-out publish names its batch and how long it ran, so production can size the stall."""
    monkeypatch.setattr(native_publish, "REQUEST_TIMEOUT", 0.2)
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.3)
    monkeypatch.setattr(native_publish, "LEG_CLOSE_TIMEOUT", 0.2)
    daemon = _Daemon()
    daemon.mode = "blackholed"
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        await asyncio.wait_for(publisher.publish({"type": "agent_event", "n": 1}), 2.0)
        messages = [record.getMessage() for record in caplog.records]
        diagnostic = next(m for m in messages if "Native WS publish failed" in m)
        assert "after 0.2" in diagnostic
        assert "with 1 ops" in diagnostic
        assert f"{daemon.body_sizes[0]} body bytes" in diagnostic
        assert "TimeoutError" in diagnostic
    finally:
        await publisher.stop()


async def test_server_subscriptions_precede_later_publishes_and_replies(
    supervisor_token: str,
) -> None:
    log: list[str] = []
    daemon = _Daemon(log)
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        # Python-only sockets: the same subscription set as today, already-completed, no request.
        plain = SimpleNamespace(subscriptions=None)
        done = add_server_subscriptions(plain, "a", "b")
        assert done.done()
        assert plain.subscriptions == {"a", "b"}
        assert daemon.requests == []

        # attach_to_session replies only after its leg's subscribe request is acknowledged.
        socket = _Socket(log)
        native = _adapter(publisher, socket)
        source = MagicMock()
        source.id = "source-uuid"
        source.external_id = "cli-session-123"
        source.project_id = "project-1"
        source.seq_num = 42
        source.source = "codex"
        source.title = "Observed Session"
        source.status = "active"
        source.model = "gpt-5.4"
        source.reasoning_effort = "high"
        source.chat_mode = "plan"
        source.git_branch = "main"
        source.context_window = 200000
        source.session_type = "terminal"
        source.terminal_context = {"tmux_pane": "%8"}
        source.workflow_name = None
        source.agent_run_id = None
        host = MagicMock()
        for state in (
            "_chat_sessions",
            "_active_chat_tasks",
            "_pending_modes",
            "_pending_worktree_paths",
            "_pending_agents",
            "_pending_projects",
            "_pending_providers",
            "_pending_inject_contexts",
        ):
            setattr(host, state, {})
        host.session_manager.get = MagicMock(return_value=source)
        host.completion_registry = None
        host.run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
        host.clients = {native: {"user_id": "local-web-0000abcd", "project_id": "project-1"}}
        host._send_error = AsyncMock()
        daemon.gate = asyncio.Event()
        with patch(
            "gobby.storage.sessions._contested_expiry.read_session_variables",
            return_value={},
        ):
            attach = asyncio.create_task(
                SessionControlMixin._handle_attach_to_session(
                    host, native, {"session_id": "source-uuid"}
                )
            )
            await _until(daemon.received.is_set)
            await _turns()
            assert "reply" not in log
            daemon.gate.set()
            await asyncio.wait_for(attach, 1.0)
        assert daemon.ops() == [
            {
                "op": "subscribe",
                "leg": 7,
                "events": [
                    "session_message:session_id=source-uuid",
                    "hook_event:session_id=cli-session-123",
                ],
            }
        ]
        assert log == ["ack:0", "reply"]
        assert json.loads(socket.sent[0])["type"] == "attach_to_session_result"

        # workspace_attach: waiter additions precede the fence publish, and the reply
        # follows the acknowledgement of the request carrying the handler's own subscribe.
        log.clear()
        daemon.requests.clear()
        daemon.received.clear()
        daemon.gate = asyncio.Event()
        topic = "workspace_event:workspace_id=w-1"
        fence_event = {"type": "workspace_event", "workspace_id": "w-1", "seq": 6}
        snapshot = SimpleNamespace(workspace=SimpleNamespace(id="w-1"), lifecycle_seq=5)
        loop = asyncio.get_running_loop()

        async def read_workspace(_data: dict[str, Any]) -> Any:
            # A mutation queued on the snapshot fence publishes once this read yields.
            loop.call_soon(publisher.publish, fence_event)
            return snapshot

        server = WebSocketServer(CONFIG, MagicMock(), AsyncMock(return_value="u"))
        workspace_native = _adapter(publisher, _Socket(log), leg=9)
        python_only = _PythonOnlySocket()
        request = {"type": "workspace_attach", "workspace": "w-1"}
        with (
            patch.object(server, "_read_workspace", side_effect=read_workspace),
            patch.object(
                server,
                "_leases",
                return_value=SimpleNamespace(lifecycle_snapshot=lambda: {"seq": 5}),
            ),
            patch.object(server, "_snapshot_reply", return_value={"type": "workspace_snapshot"}),
        ):
            attaches = [
                asyncio.create_task(server._handle_workspace_attach(workspace_native, request)),
                asyncio.create_task(server._handle_workspace_attach(python_only, request)),
            ]
            await _until(daemon.received.is_set)
            await _turns()
            assert "reply" not in log
            daemon.gate.set()
            await asyncio.wait_for(asyncio.gather(*attaches), 1.0)

        ops = daemon.ops()
        subscribe = {"op": "subscribe", "leg": 9, "events": [topic]}
        assert ops[0] == subscribe
        assert ops.count(subscribe) == 2
        assert ops.index(_publish_op(fence_event)) > 0
        # The handler's own subscribe rides in the last request that carries one.
        handler_request = max(
            index for index, sent in enumerate(daemon.requests) if subscribe in sent["ops"]
        )
        assert log.index("reply") > log.index(f"ack:{handler_request}")
        assert topic in workspace_native.subscriptions
        assert python_only.subscriptions == {topic}
        assert {op["leg"] for op in ops if op["op"] == "subscribe"} == {9}
        python_only.send.assert_awaited_once()
    finally:
        await publisher.stop()


async def test_native_mode_from_environment_and_bind_waits_for_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (native_publish.TOKEN_WAIT, native_publish.TOKEN_POLL_INTERVAL) == (2.0, 0.05)
    bootstrap = BootstrapConfig(daemon_port=60887, bind_host="127.0.0.1")
    # A stale environment copy of the secret never counts: only the pipe-fed seam does.
    monkeypatch.setenv("GOBBY_FRONT_DOOR_SECRET", SECRET)
    for fd, secret in [(None, None), (9, None), (None, SECRET), (9, "")]:
        monkeypatch.setattr(runner_backend, "_supervisor_fd", fd)
        monkeypatch.setattr(runner_backend, "_front_door_secret", secret)
        assert native_publisher_from_environment(CONFIG, bootstrap) is None, (fd, secret)

    monkeypatch.setattr(runner_backend, "_supervisor_fd", 9)
    monkeypatch.setattr(runner_backend, "_front_door_secret", SECRET)
    runner_backend._token.reset()
    polls = _count_token_polls(monkeypatch)
    monkeypatch.setattr(native_publish, "TOKEN_POLL_INTERVAL", 0)
    try:
        # The token arrives after init_servers built the publisher.
        daemon = _Daemon()
        publisher = native_publisher_from_environment(
            CONFIG, bootstrap, transport=httpx.MockTransport(daemon.handle)
        )
        assert publisher is not None
        assert publisher.front_door_secret == SECRET
        start = asyncio.create_task(publisher.start())
        await _until(lambda: len(polls) >= 3)
        assert set(polls) == {None}
        assert daemon.binds == []
        runner_backend._token.feed(f"{TOKEN}\n".encode())
        await asyncio.wait_for(start, 2.0)
        assert publisher.generation == GEN1
        [bind] = daemon.binds
        assert bind.url == httpx.URL("http://127.0.0.1:60887/_gobby/ws/bind")
        assert bind.headers["X-Gobby-Backend-Token"] == TOKEN
        await publisher.stop()

        ipv6 = _Daemon()
        publisher = native_publisher_from_environment(
            CONFIG,
            BootstrapConfig(daemon_port=60887, bind_host="::1"),
            transport=httpx.MockTransport(ipv6.handle),
        )
        assert publisher is not None
        await publisher.start()
        assert ipv6.binds[0].url == httpx.URL("http://[::1]:60887/_gobby/ws/bind")
        await publisher.stop()

        # A token that never appears fails the bind into the rebind loop: with no wait,
        # each bind attempt polls exactly once.
        monkeypatch.setattr(native_publish, "TOKEN_WAIT", 0)
        monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0)
        runner_backend._token.reset()
        polls.clear()
        late = _Daemon()
        publisher = native_publisher_from_environment(
            CONFIG, bootstrap, transport=httpx.MockTransport(late.handle)
        )
        assert publisher is not None
        await asyncio.wait_for(publisher.start(), 1.0)
        assert publisher.generation is None
        await _until(lambda: len(polls) >= 3)
        assert set(polls) == {None}
        assert publisher.generation is None
        assert late.binds == []
        runner_backend._token.feed(f"{TOKEN}\n".encode())
        await _until(lambda: publisher.generation == GEN1, timeout=1.0)
        await publisher.stop()
    finally:
        runner_backend._token.reset()


class _LoopbackHTTP:
    """A bare HTTP/1.1 listener on loopback that records each request and answers it."""

    def __init__(
        self,
        answer: Callable[[str], bytes],
        *,
        publish_delay: float = 0.0,
        bind_delays: Sequence[float] = (),
    ) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._answer = answer
        self._publish_delay = publish_delay
        # Delay for each successive bind attempt; attempts past the list answer at once.
        self._bind_delays = list(bind_delays)
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return int(self._server.sockets[0].getsockname()[1])

    async def __aexit__(self, *_exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
                request_line, *lines = head.split("\r\n")[:-2]
                headers = {
                    name.strip().lower(): value.strip()
                    for name, value in (line.split(":", 1) for line in lines)
                }
                await reader.readexactly(int(headers.get("content-length", "0")))
                target = request_line.split(" ")[1]
                self.requests.append((target, headers))
                if target == native_publish.PUBLISH_PATH:
                    await asyncio.sleep(self._publish_delay)
                if target == native_publish.BIND_PATH and self._bind_delays:
                    await asyncio.sleep(self._bind_delays.pop(0))
                writer.write(self._answer(target))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            # A client that cut a held request has closed its socket; there is no answer to give.
            pass
        finally:
            writer.close()


def _daemon_answer(target: str) -> bytes:
    if target == "/_gobby/ws/bind":
        body = json.dumps({"generation": GEN1}).encode()
        head = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}"
        return f"{head}\r\n\r\n".encode() + body
    return b"HTTP/1.1 204 No Content\r\n\r\n"


async def test_production_client_ignores_proxy_environment(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bind and publish go straight to loopback even when proxy variables are set."""
    proxy_refusal = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    daemon = _LoopbackHTTP(_daemon_answer)
    proxy = _LoopbackHTTP(lambda _target: proxy_refusal)
    async with daemon as daemon_port, proxy as proxy_port:
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        # Lower-case names take precedence, so both spellings point at the recording proxy.
        for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy_port}")
        monkeypatch.setattr(runner_backend, "_supervisor_fd", 9)
        monkeypatch.setattr(runner_backend, "_front_door_secret", SECRET)
        publisher = native_publisher_from_environment(
            CONFIG, BootstrapConfig(daemon_port=daemon_port, bind_host="127.0.0.1")
        )
        assert publisher is not None
        try:
            await asyncio.wait_for(publisher.start(), 2.0)
            assert publisher.generation == GEN1
            await asyncio.wait_for(publisher.publish({"type": "agent_event"}), 2.0)
            assert publisher.generation == GEN1
        finally:
            await publisher.stop()

    assert proxy.requests == []
    assert [target for target, _headers in daemon.requests] == [
        "/_gobby/ws/bind",
        "/_gobby/ws/publish",
    ]
    for _target, headers in daemon.requests:
        assert headers["x-gobby-front-door"] == SECRET
        assert headers["x-gobby-backend-token"] == supervisor_token


async def test_a_bind_held_past_the_deadline_is_retried_until_the_front_door_answers(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binds held past the deadline cost retries, and the first timely answer rebinds the door.

    Each held attempt is cut at the deadline; the rebind loop then waits its interval and
    tries again. A front door that answers late is therefore recovered within a bounded
    number of attempts, over real sockets and real timers.
    """
    monkeypatch.setattr(native_publish, "REQUEST_TIMEOUT", 0.3)
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.05)
    daemon = _LoopbackHTTP(_daemon_answer, bind_delays=[1.0, 1.0])
    async with daemon as daemon_port:
        monkeypatch.setattr(runner_backend, "_supervisor_fd", 9)
        monkeypatch.setattr(runner_backend, "_front_door_secret", SECRET)
        publisher = native_publisher_from_environment(
            CONFIG, BootstrapConfig(daemon_port=daemon_port, bind_host="127.0.0.1")
        )
        assert publisher is not None
        try:
            await asyncio.wait_for(publisher.start(), 2.0)
            # Hang guards only. Host load can stretch each attempt, so the held attempts are
            # asserted by count below, never by elapsed time.
            await _until(lambda: publisher.generation == GEN1, timeout=5.0)
            binds = [target for target, _headers in daemon.requests if target == "/_gobby/ws/bind"]
            assert len(binds) >= 3
            assert await publisher.wait_bound(timeout=0.0) is True
        finally:
            await publisher.stop()


async def test_a_stalled_loop_is_reported_while_the_publisher_runs(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The sampler names a loop stall as it happens, with the lag it measured.

    The stall is virtual, as in the stall test above: the loop clock jumps while the sampler
    sleeps, so the sample overshoots by the jump.
    """
    monkeypatch.setattr(native_publish, "LOOP_LAG_INTERVAL", 0.05)
    loop = asyncio.get_running_loop()
    real_time = loop.time
    stalled = 0.0
    monkeypatch.setattr(loop, "time", lambda: real_time() + stalled)
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        await asyncio.sleep(0)  # The sampler registers its first sleep.
        stalled += 2.0
        await _until(lambda: any("event loop lag" in r.getMessage() for r in caplog.records))
    finally:
        await publisher.stop()
    lag = next(r.getMessage() for r in caplog.records if "event loop lag" in r.getMessage())
    measured = float(lag.split("lag ")[1].split("s ")[0])
    assert measured >= 1.5  # The 2 s jump registers; the sample starts a few ms after it.


@pytest.mark.asyncio
async def test_server_start_reports_a_stalled_loop(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The production server start owns the sampler. publisher.start is not required."""
    monkeypatch.setattr(native_publish, "LOOP_LAG_INTERVAL", 0.05)
    loop = asyncio.get_running_loop()
    real_time = loop.time
    stalled = 0.0
    monkeypatch.setattr(loop, "time", lambda: real_time() + stalled)
    server, _shutdown = _running_server(_publisher(_Daemon()), monkeypatch)
    await server.start()
    try:
        await asyncio.sleep(0)  # The sampler registers its first sleep.
        stalled += 2.0
        await _until(lambda: any("event loop lag" in r.getMessage() for r in caplog.records))
    finally:
        await server.stop()
    lag = next(r.getMessage() for r in caplog.records if "event loop lag" in r.getMessage())
    measured = float(lag.split("lag ")[1].split("s ")[0])
    assert measured >= 1.5


@pytest.mark.asyncio
async def test_server_start_rebinds_after_a_transient_bind_failure(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed startup bind stays visible and the publisher rebinds within its interval."""
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.01)
    daemon = _Daemon()
    daemon.bind_status = [503]
    publisher = _publisher(daemon)
    server, shutdown = _running_server(publisher, monkeypatch)
    with pytest.raises(Exception, match="bind answered 503"):
        await server.start()
    try:
        await _until(lambda: publisher.generation == GEN1)
    finally:
        await publisher.stop()
    shutdown.assert_not_awaited()
    assert any("Native WS bind failed" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_server_start_rebind_keeps_lifecycle_publication(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recovered startup bind leaves lifecycle publication and idle cleanup live."""
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.01)
    daemon = _Daemon()
    daemon.bind_status = [503]
    publisher = _publisher(daemon)
    registry = TerminalLeaseRegistry()
    server = WebSocketServer(CONFIG, MagicMock(), AsyncMock(return_value="u"))
    server.native_publisher = publisher
    server.lease_registry = registry
    with pytest.raises(Exception, match="bind answered 503"):
        await server.start()
    try:
        assert await publisher.wait_bound(timeout=2.0) is True

        async def record(_stamped: dict[str, Any]) -> None:
            return None

        pending = {"type": "terminal_event", "event": "pending"}
        stamped = await asyncio.wait_for(registry.publish_lifecycle(pending, record), 1.0)
        assert stamped["event"] == "pending"
        assert server._cleanup_task is not None
        assert not server._cleanup_task.done()
    finally:
        await server.stop()


@pytest.mark.parametrize(
    ("bind_delay", "deadline", "outcome", "first_byte_seen"),
    [
        (1.0, 0.3, "bind failed after", False),
        (0.6, 2.0, "bind answered after", True),
    ],
)
async def test_a_slow_bind_names_the_phase_it_reached(
    bind_delay: float,
    deadline: float,
    outcome: str,
    first_byte_seen: bool,
    supervisor_token: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bind log says how far the request got: a held answer shows no first byte."""
    monkeypatch.setattr(native_publish, "REQUEST_TIMEOUT", deadline)
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.05)
    daemon = _LoopbackHTTP(_daemon_answer, bind_delays=[bind_delay])
    async with daemon as daemon_port:
        monkeypatch.setattr(runner_backend, "_supervisor_fd", 9)
        monkeypatch.setattr(runner_backend, "_front_door_secret", SECRET)
        publisher = native_publisher_from_environment(
            CONFIG, BootstrapConfig(daemon_port=daemon_port, bind_host="127.0.0.1")
        )
        assert publisher is not None
        try:
            await asyncio.wait_for(publisher.start(), 2.0)
            await _until(lambda: publisher.generation == GEN1)
        finally:
            await publisher.stop()
    line = next(r.getMessage() for r in caplog.records if outcome in r.getMessage())
    assert "phases" in line
    assert "connection.connect_tcp" in line
    assert ("http11.receive_response_headers" in line) is first_byte_seen


@pytest.mark.parametrize(
    ("publish_delay", "deadline", "outcome", "first_byte_seen"),
    [
        (1.0, 0.3, "publish failed for generation", False),
        (0.6, 2.0, "publish answered after", True),
    ],
)
async def test_a_slow_publish_names_the_phase_it_reached(
    publish_delay: float,
    deadline: float,
    outcome: str,
    first_byte_seen: bool,
    supervisor_token: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The publish log says how far the request got, as the bind log does."""
    monkeypatch.setattr(native_publish, "REQUEST_TIMEOUT", deadline)
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.05)
    daemon = _LoopbackHTTP(_daemon_answer, publish_delay=publish_delay)
    async with daemon as daemon_port:
        monkeypatch.setattr(runner_backend, "_supervisor_fd", 9)
        monkeypatch.setattr(runner_backend, "_front_door_secret", SECRET)
        publisher = native_publisher_from_environment(
            CONFIG, BootstrapConfig(daemon_port=daemon_port, bind_host="127.0.0.1")
        )
        assert publisher is not None
        try:
            await asyncio.wait_for(publisher.start(), 2.0)
            await _until(lambda: publisher.generation == GEN1)
            publisher.publish({"type": "agent_event"})
            await _until(lambda: any(outcome in r.getMessage() for r in caplog.records), 5.0)
        finally:
            await publisher.stop()
    line = next(r.getMessage() for r in caplog.records if outcome in r.getMessage())
    assert "phases" in line
    # Publish reuses the connection the bind opened, so no connect phase is logged.
    assert "http11.send_request_headers" in line
    assert ("http11.receive_response_headers" in line) is first_byte_seen


async def test_a_held_rebind_is_logged_as_a_rebind(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A rebind that the front door holds past the deadline logs as a rebind, with phases."""
    monkeypatch.setattr(native_publish, "REQUEST_TIMEOUT", 0.3)
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.05)
    daemon = _LoopbackHTTP(_daemon_answer, bind_delays=[1.0, 1.0])
    async with daemon as daemon_port:
        monkeypatch.setattr(runner_backend, "_supervisor_fd", 9)
        monkeypatch.setattr(runner_backend, "_front_door_secret", SECRET)
        publisher = native_publisher_from_environment(
            CONFIG, BootstrapConfig(daemon_port=daemon_port, bind_host="127.0.0.1")
        )
        assert publisher is not None
        try:
            await asyncio.wait_for(publisher.start(), 2.0)
            await _until(lambda: publisher.generation == GEN1, 5.0)
        finally:
            await publisher.stop()
    assert any(
        "rebind failed after" in r.getMessage() and "phases" in r.getMessage()
        for r in caplog.records
    )


async def test_the_probe_wrapper_serves_a_rebind_and_records_its_generation(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The e2e probe wraps bind; a rebind through that wrapper must bind and be recorded."""
    monkeypatch.setattr(native_publish, "REQUEST_TIMEOUT", 0.3)
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.05)
    monkeypatch.setattr(NativePublisher, "bind", recording_bind(NativePublisher.bind, tmp_path))
    daemon = _LoopbackHTTP(_daemon_answer, bind_delays=[1.0])
    async with daemon as daemon_port:
        monkeypatch.setattr(runner_backend, "_supervisor_fd", 9)
        monkeypatch.setattr(runner_backend, "_front_door_secret", SECRET)
        publisher = native_publisher_from_environment(
            CONFIG, BootstrapConfig(daemon_port=daemon_port, bind_host="127.0.0.1")
        )
        assert publisher is not None
        try:
            await asyncio.wait_for(publisher.start(), 2.0)
            await _until(lambda: publisher.generation == GEN1, 5.0)
        finally:
            await publisher.stop()
    records = (tmp_path / RECORDS_FILE).read_text().splitlines()
    assert [json.loads(line)["generation"] for line in records] == [GEN1]


@pytest.mark.slow
async def test_a_publish_slower_than_httpx_default_stays_bound_inside_the_deadline(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 6 s answer over a real socket is inside the 15 s deadline, so the door stays bound.

    Socket phases used to keep httpx's 5 s default, which fired first as ReadTimeout and
    retired the generation, so the deadline raised in _post never governed them.
    """
    daemon = _LoopbackHTTP(_daemon_answer, publish_delay=6.0)
    async with daemon as daemon_port:
        monkeypatch.setattr(runner_backend, "_supervisor_fd", 9)
        monkeypatch.setattr(runner_backend, "_front_door_secret", SECRET)
        publisher = native_publisher_from_environment(
            CONFIG, BootstrapConfig(daemon_port=daemon_port, bind_host="127.0.0.1")
        )
        assert publisher is not None
        try:
            await asyncio.wait_for(publisher.start(), 2.0)
            await asyncio.wait_for(publisher.publish({"type": "agent_event"}), 15.0)
            assert publisher.generation == GEN1
        finally:
            await publisher.stop()

    assert [target for target, _headers in daemon.requests][-1] == "/_gobby/ws/publish"


@pytest.mark.parametrize("budget", ["bytes", "ops", "futures"])
async def test_publisher_admission_budgets_fail_generation_on_overflow(
    budget: str, supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        native_publish.MAX_RETAINED_BYTES,
        native_publish.MAX_RETAINED_OPS,
        native_publish.MAX_WAITING_FUTURES,
    ) == (134_217_728, 65_536, 65_536)

    def message(kind: str, n: int) -> dict[str, Any]:
        return {"type": kind, "n": n}

    size = len(json_dumps(_publish_op(message("burst", 0))).encode())
    limit = {
        "bytes": "MAX_RETAINED_BYTES",
        "ops": "MAX_RETAINED_OPS",
        "futures": "MAX_WAITING_FUTURES",
    }
    monkeypatch.setattr(native_publish, limit[budget], 3 * size if budget == "bytes" else 3)
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.2)
    monkeypatch.setattr(native_publish, "LEG_CLOSE_TIMEOUT", 0.2)
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        leg = _Leg()
        assert publisher.register_leg(GEN1, leg)
        daemon.gate = asyncio.Event()
        futures = [publisher.publish(message("burst", 0))]
        await _until(daemon.received.is_set)

        async def produce(n: int) -> asyncio.Future[None]:
            await asyncio.sleep(0)
            return publisher.publish(message("burst", n))

        futures.extend(await asyncio.gather(produce(1), produce(2)))
        await _turns()
        assert (publisher.retained_bytes, publisher.retained_ops) == (3 * size, 3)
        [overflow] = await asyncio.gather(produce(3))
        await asyncio.wait_for(overflow, 1.0)
        assert await asyncio.wait_for(asyncio.gather(*futures), 1.0) == [None, None, None]
        assert publisher.generation != GEN1
        assert (publisher.retained_bytes, publisher.retained_ops) == (0, 0)
        await _until(lambda: leg.closed == [1011], timeout=1.0)

        await _until(lambda: publisher.generation == GEN2, timeout=1.0)
        daemon.gate.set()
        daemon.gate = None
        newer = _Leg()
        assert publisher.register_leg(GEN2, newer)
        for n in range(10):
            await asyncio.wait_for(publisher.publish(message("steady", n % 10)), 1.0)
        assert daemon.ops(GEN1) == [_publish_op(message("burst", 0))]
        assert all(op["filter"]["type"] == "steady" for op in daemon.ops(GEN2))
        assert (publisher.retained_bytes, publisher.retained_ops) == (0, 0)
        assert publisher.generation == GEN2
        assert newer.closed == []

        daemon.gate = asyncio.Event()
        stalled = [publisher.publish(message("steady", n)) for n in range(2)]
        await _until(lambda: publisher.retained_ops == 2)
        await publisher.stop()
        assert all(future.done() and future.result() is None for future in stalled)
        assert (publisher.retained_bytes, publisher.retained_ops) == (0, 0)
    finally:
        await publisher.stop()


async def test_frame_that_fails_to_encode_queues_no_op(supervisor_token: str) -> None:
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        # The first frame encodes; the second cannot. Neither op may be queued.
        # Only the first frame has a size to charge; the second cannot be encoded at all.
        bad = publisher.publish(
            {"type": "burst"},
            [{"seq": 1}, {"seq": 2, "data": object()}],
            source_bytes=_source_bytes([{"seq": 1}]),
        )
        with pytest.raises(TypeError):
            await asyncio.wait_for(bad, 1.0)
        assert (publisher.retained_bytes, publisher.retained_ops) == (0, 0)
        await _turns()
        assert daemon.requests == []

        # The failed call leaves the queue order intact for the calls after it.
        await asyncio.wait_for(publisher.publish({"type": "ok"}), 1.0)
        assert [op["filter"]["type"] for op in daemon.ops(GEN1)] == ["ok"]
    finally:
        await publisher.stop()


async def test_canonical_json_async_bytes_match_canonical_json() -> None:
    event = {
        "type": "workspace_event",
        "seq": 7,
        "flag": True,
        "none": None,
        "ratio": 0.1,
        "big": 2**40,
        "empty": {"list": [], "dict": {}},
        "text": 'quote " slash \\ tab \t nul \x00 unit   é 😀',
        "huge": "é" * (2 * 1024 * 1024),
        "nested": {"b": [1, {"d": "x", "c": "y"}], "a": ("t", None)},
    }
    assert await ws_protocol.canonical_json_async(event) == canonical_json(event)


@pytest.mark.parametrize("bad", [{"seq": 2**53}, {"outer": [{"message_seq": -1}]}])
async def test_canonical_json_async_rejects_unsafe_integers_as_canonical_json_does(
    bad: dict[str, Any],
) -> None:
    with pytest.raises(ws_protocol.SafeIntegerOverflowError):
        canonical_json(bad)
    with pytest.raises(ws_protocol.SafeIntegerOverflowError):
        await ws_protocol.canonical_json_async(bad)


def test_iter_fragment_event_frames_match_fragment_event() -> None:
    raw = canonical_json({"type": "workspace_event", "seq": 42, "text": "é" * 1_000_000})
    expected = fragment_event(
        event="workspace_event",
        terminal_id="ws-1",
        attachment_id="ws-1",
        message_seq=42,
        complete_json=raw,
    )
    assert len(expected) > 1
    frames = ws_protocol.iter_fragment_event(
        event="workspace_event",
        terminal_id="ws-1",
        attachment_id="ws-1",
        message_seq=42,
        complete_json=raw,
    )
    assert list(frames) == expected


async def test_fragment_event_async_frames_match_fragment_event() -> None:
    event = {"type": "workspace_event", "seq": 42, "text": "é" * 1_000_000}
    expected = fragment_event(
        event="workspace_event",
        terminal_id="ws-1",
        attachment_id="ws-1",
        message_seq=42,
        complete_json=canonical_json(event),
    )
    assert len(expected) > 1
    # canonical_json_async returns a bytearray, the buffer the publisher fragments.
    raw = await ws_protocol.canonical_json_async(event)
    frames = await ws_protocol.fragment_event_async(
        event="workspace_event",
        terminal_id="ws-1",
        attachment_id="ws-1",
        message_seq=42,
        complete_json=raw,
    )
    assert frames == expected


async def test_canonical_json_async_pauses_once_per_piece_budget() -> None:
    value = {"text": "é" * (4 * ws_protocol.CANONICAL_PIECE_CHARS)}
    pieces = list(ws_protocol._canonical_pieces(value))
    assert max(len(piece) for piece in pieces) <= ws_protocol.CANONICAL_PIECE_CHARS
    pauses: list[int] = []

    async def counting_pause() -> None:
        pauses.append(1)
        await asyncio.sleep(0)

    encoded = await ws_protocol.canonical_json_async(value, pause=counting_pause)
    assert encoded == canonical_json(value)
    total_chars = sum(len(piece) for piece in pieces)
    assert len(pauses) >= total_chars // ws_protocol.CANONICAL_PIECE_CHARS


async def test_large_publish_keeps_its_place_before_later_calls(supervisor_token: str) -> None:
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        big = publisher.publish(
            {"type": "big"},
            [{"type": "big", "text": "é" * (2 * ws_protocol.CANONICAL_PIECE_CHARS)}],
            # Each "é" is two bytes of UTF-8 source.
            source_bytes=4 * ws_protocol.CANONICAL_PIECE_CHARS,
        )
        subscribed = publisher.subscribe(7, ["terminal.output"])
        small = publisher.publish({"type": "small"})
        # The big encode yields to the loop, so the later calls are made while it is pending.
        assert not big.done()
        await asyncio.wait_for(asyncio.gather(big, subscribed, small), 2.0)
        labels = [op.get("filter", {}).get("type", op.get("op")) for op in daemon.ops(GEN1)]
        assert labels == ["big", "subscribe", "small"]
    finally:
        await publisher.stop()


def _hold_encodes(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """Keep every publish's encoding pending until the returned event is set."""
    gate = asyncio.Event()
    encode = native_publish._publish_ops

    async def held(filter_json: str, sources: Any) -> list[bytes]:
        await gate.wait()
        return await encode(filter_json, sources)

    monkeypatch.setattr(native_publish, "_publish_ops", held)
    return gate


async def test_cancelled_waiter_keeps_later_calls_behind_its_predecessor(
    supervisor_token: str,
) -> None:
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        big = publisher.publish(
            {"type": "big"},
            [{"type": "big", "text": "é" * (2 * ws_protocol.CANONICAL_PIECE_CHARS)}],
            # Each "é" is two bytes of UTF-8 source.
            source_bytes=4 * ws_protocol.CANONICAL_PIECE_CHARS,
        )
        waiter = publisher.publish({"type": "waiter"})
        # One loop turn lets big start encoding and the waiter block on big's place.
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        small = publisher.publish({"type": "small"})
        await asyncio.wait_for(asyncio.gather(big, small), 2.0)
        labels = [op.get("filter", {}).get("type", op.get("op")) for op in daemon.ops(GEN1)]
        assert labels == ["big", "small"]
    finally:
        await publisher.stop()


async def test_call_cancelled_before_it_starts_leaves_no_op_coroutine(
    supervisor_token: str,
) -> None:
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        big = publisher.publish(
            {"type": "big"},
            [{"type": "big", "text": "é" * (2 * ws_protocol.CANONICAL_PIECE_CHARS)}],
            # Each "é" is two bytes of UTF-8 source.
            source_bytes=4 * ws_protocol.CANONICAL_PIECE_CHARS,
        )
        cancelled = publisher.publish({"type": "cancelled"})
        cancelled.cancel()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            gc.collect()
        after = publisher.publish({"type": "after"})
        await asyncio.wait_for(asyncio.gather(big, after), 2.0)
        assert [str(w.message) for w in caught if "never awaited" in str(w.message)] == []
        labels = [op.get("filter", {}).get("type", op.get("op")) for op in daemon.ops(GEN1)]
        assert labels == ["big", "after"]
    finally:
        await publisher.stop()


async def test_preparations_over_the_waiting_cap_fail_the_generation(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(native_publish, "MAX_WAITING_FUTURES", 2)
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.2)
    gate = _hold_encodes(monkeypatch)
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        held = [publisher.publish({"type": "held", "n": n}) for n in range(2)]
        # Both held calls wait for encoding and count as waiting, so the third is refused
        # without building any op.
        refused = publisher.publish({"type": "refused"})
        assert refused.done() and refused.result() is None
        assert publisher.generation is None
        gate.set()
        await asyncio.wait_for(asyncio.gather(*held), 2.0)
        assert daemon.ops(GEN1) == []
        assert publisher.retained_ops == 0
    finally:
        await publisher.stop()


async def test_cap_holds_while_a_call_expands_into_queued_ops(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One op per frame, and room for three places: the expanding call and two waiting behind it.
    monkeypatch.setattr(native_publish, "PUBLISH_CHUNK", 1)
    monkeypatch.setattr(native_publish, "MAX_RETAINED_OPS", 3)
    monkeypatch.setattr(native_publish, "MAX_WAITING_FUTURES", 3)
    gate = _hold_encodes(monkeypatch)
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    places: list[int] = []

    async def watch() -> None:
        while True:
            places.append(len(publisher._preparing) + publisher.retained_ops)
            await asyncio.sleep(0)

    watcher = asyncio.create_task(watch())
    try:
        frames = [{"n": n} for n in range(3)]
        expanding = publisher.publish({"type": "burst"}, frames, source_bytes=_source_bytes(frames))
        waiting = [publisher.publish({"type": "waiter", "n": n}) for n in range(2)]
        await _turns()
        gate.set()
        assert await asyncio.wait_for(asyncio.gather(expanding, *waiting), 2.0) == [None] * 3
        assert publisher.generation != GEN1
        # The expanding call's three ops would join the two calls still waiting behind it, so
        # the call is refused before any op is queued.
        assert daemon.requests == []
        assert publisher.retained_ops == 0
        assert max(places) <= 3
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await publisher.stop()


async def test_source_bytes_hit_the_byte_cap_before_the_count_cap(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(native_publish, "MAX_RETAINED_BYTES", 10_000)
    gate = _hold_encodes(monkeypatch)
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        frames = [{"data": "a" * 4_000}]
        held = publisher.publish({"type": "large"}, frames, source_bytes=6_000)
        # Two 6,000-byte sources exceed the 10,000-byte cap with two places in use. The count
        # caps are far above two, so the refusal comes from the byte cap at admission.
        refused = publisher.publish({"type": "large"}, frames, source_bytes=6_000)
        assert refused.done() and refused.result() is None
        assert publisher.generation != GEN1
        gate.set()
        await asyncio.wait_for(held, 2.0)
        assert daemon.requests == []
    finally:
        await publisher.stop()


async def test_exchange_refuses_encoded_bytes_that_exceed_the_queued_bytes(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(native_publish, "MAX_RETAINED_BYTES", 10_000)
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        # Holding the POST keeps the first op's bytes queued.
        gate = asyncio.Event()
        daemon.gate = gate
        first = publisher.publish({"type": "first"}, [{"data": "a" * 6_000}], source_bytes=10)
        await _until(daemon.received.is_set)
        # The declared source is small, so admission passes. The encoded op does not fit beside
        # the queued first op, so the exchange refuses it.
        over = publisher.publish({"type": "over"}, [{"data": "b" * 6_000}], source_bytes=10)
        await asyncio.wait_for(over, 1.0)
        assert publisher.generation != GEN1
        await asyncio.wait_for(first, 1.0)
        assert len(daemon.requests) == 1
        assert (publisher.retained_bytes, publisher.retained_ops) == (0, 0)
    finally:
        gate.set()
        await publisher.stop()


async def test_exchange_counts_the_source_bytes_of_calls_still_waiting(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(native_publish, "MAX_RETAINED_BYTES", 10_000)
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        # The first call encodes to about 4,600 bytes from a 3,000-byte source. The second waits
        # behind it with a 6,000-byte source reserved, so 4,600 encoded bytes do not fit beside it.
        first = publisher.publish({"type": "first"}, [{"data": "a" * 4_500}], source_bytes=3_000)
        second = publisher.publish({"type": "second"}, [{"data": "b"}], source_bytes=6_000)
        await asyncio.wait_for(asyncio.gather(first, second), 2.0)
        assert publisher.generation != GEN1
        assert daemon.requests == []
    finally:
        await publisher.stop()


async def test_reserved_source_bytes_return_at_the_exchange_or_on_cancel(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(native_publish, "MAX_RETAINED_BYTES", 10_000)
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        sent = publisher.publish({"type": "sent"}, [{"data": "a"}], source_bytes=6_000)
        await asyncio.wait_for(sent, 1.0)
        # The 6,000 bytes went back at the exchange, so a 9,000-byte source is admitted.
        nxt = publisher.publish({"type": "next"}, [{"data": "b"}], source_bytes=9_000)
        await asyncio.wait_for(nxt, 1.0)
        cancelled = publisher.publish({"type": "cancelled"}, [{"data": "c"}], source_bytes=6_000)
        cancelled.cancel()
        await _turns()
        # A cancelled call returns its reservation too.
        after = publisher.publish({"type": "after"}, [{"data": "d"}], source_bytes=9_000)
        await asyncio.wait_for(after, 1.0)
        assert publisher.generation == GEN1
        assert [op["filter"]["type"] for op in daemon.ops(GEN1)] == ["sent", "next", "after"]
    finally:
        await publisher.stop()


async def test_stop_cancels_calls_still_preparing(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold_encodes(monkeypatch)
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    first = publisher.publish({"type": "first"})
    second = publisher.publish({"type": "second"})
    await _turns()
    await asyncio.wait_for(publisher.stop(), 2.0)
    assert first.cancelled() and second.cancelled()
    assert publisher.retained_ops == 0
    assert daemon.ops(GEN1) == []


async def test_bind_token_poll_stays_inside_token_wait_while_large_events_encode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A publish's encode runs on the same loop as the rebind poll: the poll must still give up
    # at TOKEN_WAIT, with one poll per TOKEN_POLL_INTERVAL and no extra polls.
    monkeypatch.setattr(native_publish, "TOKEN_WAIT", 0.5)
    monkeypatch.setattr(native_publish, "TOKEN_POLL_INTERVAL", 0.05)
    runner_backend._token.reset()
    polls = _count_token_polls(monkeypatch)
    event = {"type": "workspace_event", "text": "é" * (2 * 1024 * 1024)}
    stop = asyncio.Event()
    encodes = 0

    async def encode_until_stopped() -> None:
        nonlocal encodes
        while not stop.is_set():
            await ws_protocol.canonical_json_async(event)
            encodes += 1

    daemon = _Daemon()
    publisher = _publisher(daemon)
    encoder = asyncio.create_task(encode_until_stopped())
    try:
        with pytest.raises(native_publish._BindFailed):
            await asyncio.wait_for(publisher.bind(), native_publish.TOKEN_WAIT + 1.0)
    finally:
        stop.set()
        await encoder
        await publisher.stop()
    budget = native_publish.TOKEN_WAIT / native_publish.TOKEN_POLL_INTERVAL + 1
    assert encodes >= 1
    assert set(polls) == {None}
    assert 2 <= len(polls) <= budget
    assert daemon.binds == []


async def test_loop_stall_under_host_load_keeps_the_front_door_bound(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publish answered after a host-load loop stall completes; the door stays bound.

    The stall is virtual: the loop clock jumps 3 s past the publish deadline while the request
    is parked, as if the loop had been starved. The overdue deadline fires before the response
    is released, the order the production stall produced. Production constants apply.
    """
    loop = asyncio.get_running_loop()
    real_time = loop.time
    stalled = 0.0
    monkeypatch.setattr(loop, "time", lambda: real_time() + stalled)
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        assert publisher.generation == GEN1
        daemon.gate = asyncio.Event()
        pending = publisher.publish({"type": "agent_event", "n": 1})
        await _until(daemon.received.is_set)
        stalled += 3.0  # The loop was parked this long; the response is not yet read.
        await asyncio.sleep(0)  # The loop resumes and runs the overdue publish deadline first.
        daemon.gate.set()
        await asyncio.wait_for(pending, 5.0)
        assert publisher.generation == GEN1
        assert daemon.ops() == [_publish_op({"type": "agent_event", "n": 1})]
    finally:
        await publisher.stop()


async def test_wait_bound_returns_once_a_rebind_lands(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.05)
    daemon = _Daemon()
    daemon.bind_status = [503]
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        assert publisher.generation is None
        assert await publisher.wait_bound(1.0) is True
        assert publisher.generation == GEN1
    finally:
        await publisher.stop()


async def test_wait_bound_gives_up_at_its_deadline(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 0.05)
    daemon = _Daemon()
    daemon.bind_status = [503] * 1000
    publisher = _publisher(daemon)
    await publisher.start()
    try:
        assert await publisher.wait_bound(0.2) is False
        assert publisher.generation is None
    finally:
        await publisher.stop()


async def test_wait_bound_refuses_at_once_on_a_stopped_publisher(supervisor_token: str) -> None:
    daemon = _Daemon()
    publisher = _publisher(daemon)
    await publisher.start()
    await publisher.stop()
    # A stopped publisher never binds again, so spawn must not wait out the full deadline.
    async with asyncio.timeout(1.0):
        assert await publisher.wait_bound() is False


def test_front_door_wait_covers_two_bind_attempts_and_their_rebind_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_publish, "REQUEST_TIMEOUT", 15.0)
    monkeypatch.setattr(native_publish, "REBIND_INTERVAL", 5.0)
    assert native_publish.front_door_wait() == 40.0
