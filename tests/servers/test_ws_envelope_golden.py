"""Golden corpus for WebSocket subscribe, unsubscribe and broadcast envelopes.

The existing Python handlers are the oracle: every case drives real
``WebSocketServer.handle_connection`` loops for fake direct-listener and
browser ASGI clients. The native transport replays the same corpus.
Expected output is hand-authored and is never re-recorded by this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocket

from gobby.servers.websocket.asgi_adapter import ASGIWebSocketAdapter
from gobby.servers.websocket.server import WebSocketServer
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.utils.json_helpers import json_dumps

pytestmark = pytest.mark.unit

CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "ws_envelope_golden"
SCHEMA_VERSION = 1
ACK_TYPES = frozenset({"subscribe_success", "unsubscribe_success"})
SERVER_LOGGER = "gobby.servers.websocket.server"
HANDLER_ERROR_LOG = "Message handling error for client"
STEP_TIMEOUT_SECONDS = 5.0


class CorpusError(ValueError):
    """The corpus on disk does not match the declared manifest."""


def load_corpus(directory: Path) -> dict[str, dict[str, Any]]:
    """Return the declared cases in manifest order, failing on any drift."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    document = json.loads((directory / manifest["cases_file"]).read_text(encoding="utf-8"))
    for label, doc in (("manifest", manifest), ("cases", document)):
        if doc.get("schema_version") != SCHEMA_VERSION:
            raise CorpusError(f"{label} schema_version {doc.get('schema_version')!r} is unknown")
    declared: list[str] = manifest["cases"]
    if len(set(declared)) != len(declared):
        raise CorpusError("manifest declares a case more than once")
    cases: dict[str, dict[str, Any]] = {}
    for case in document["cases"]:
        if case["name"] in cases:
            raise CorpusError(f"case {case['name']!r} is defined more than once")
        cases[case["name"]] = case
    missing = sorted(set(declared) - cases.keys())
    unlisted = sorted(cases.keys() - set(declared))
    if missing or unlisted:
        raise CorpusError(f"inventory drift: missing={missing} unlisted={unlisted}")
    return {name: cases[name] for name in declared}


def _declared_case_names() -> list[str]:
    manifest = json.loads((CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
    return list(manifest["cases"])


class _Wire:
    """Inbound queue and outbound capture shared by both fake transports."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[str] = []
        # Set whenever the connection loop asks for its next inbound frame.
        self.idle = asyncio.Event()

    async def next_inbound(self) -> str | None:
        self.idle.set()
        return await self.inbox.get()

    async def deliver(self, text: str | None) -> None:
        self.idle.clear()
        self.inbox.put_nowait(text)
        if text is not None:
            await asyncio.wait_for(self.idle.wait(), STEP_TIMEOUT_SECONDS)


class _DirectSocket:
    """The direct listener's connection: it carries no subscriptions attribute."""

    def __init__(self, name: str, wire: _Wire) -> None:
        self.user_id = f"user-{name}"
        self.latency = 0.0
        self.remote_address = ("127.0.0.1", 0)
        self._wire = wire

    async def send(self, message: str) -> None:
        self._wire.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason

    def __aiter__(self) -> _DirectSocket:
        return self

    async def __anext__(self) -> str:
        text = await self._wire.next_inbound()
        if text is None:
            raise StopAsyncIteration
        return text


class _StarletteSocket:
    """The ASGI WebSocket that the browser entry wraps in ASGIWebSocketAdapter."""

    def __init__(self, wire: _Wire) -> None:
        self.client = ("127.0.0.1", 0)
        self._wire = wire

    async def accept(self) -> None:
        return None

    async def send_text(self, data: str) -> None:
        self._wire.sent.append(data)

    async def send_bytes(self, data: bytes) -> None:
        raise AssertionError(f"unexpected binary frame: {data!r}")

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        del code, reason

    async def receive(self) -> dict[str, Any]:
        text = await self._wire.next_inbound()
        if text is None:
            return {"type": "websocket.disconnect", "code": 1000}
        return {"type": "websocket.receive", "text": text}


@dataclass
class _Connection:
    name: str
    socket: Any
    wire: _Wire
    task: asyncio.Task[None] | None = None
    seen: int = 0

    def take(self) -> list[str]:
        frames = self.wire.sent[self.seen :]
        self.seen = len(self.wire.sent)
        return frames


def _server() -> WebSocketServer:
    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    server = WebSocketServer(config, MagicMock(), AsyncMock(return_value="test-user"))
    # Disconnect cleanup finalizes the socket's attachments in the lease registry.
    server.lease_registry = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    return server


def _subscription_state(socket: Any) -> Any:
    """Encode the corpus state: "absent", None for explicit null, or sorted members."""
    if not hasattr(socket, "subscriptions"):
        return "absent"
    subscriptions = socket.subscriptions
    return None if subscriptions is None else sorted(subscriptions)


async def _connect(server: WebSocketServer, name: str, spec: dict[str, Any]) -> _Connection:
    wire = _Wire()
    socket: Any
    if spec["entry"] == "direct":
        socket = _DirectSocket(name, wire)
    elif spec["entry"] == "browser":
        socket = ASGIWebSocketAdapter(
            cast(WebSocket, _StarletteSocket(wire)), user_id=f"user-{name}"
        )
        await socket.accept()
    else:
        raise CorpusError(f"client {name!r} has unknown entry {spec['entry']!r}")
    if spec.get("explicit_null", False):
        # The internal explicit-null state that no public entry produces on its own.
        socket.subscriptions = None
    connection = _Connection(name=name, socket=socket, wire=wire)
    connection.task = asyncio.create_task(server.handle_connection(socket))
    try:
        await asyncio.wait_for(wire.idle.wait(), STEP_TIMEOUT_SECONDS)
        welcome = connection.take()
        assert [json.loads(frame)["type"] for frame in welcome] == ["connection_established"]
        assert _subscription_state(socket) == spec["initial_state"], name
    except BaseException:
        connection.task.cancel()
        raise
    return connection


async def _disconnect(server: WebSocketServer, connection: _Connection) -> None:
    await connection.wire.deliver(None)
    assert connection.task is not None
    await asyncio.wait_for(connection.task, STEP_TIMEOUT_SECONDS)
    assert connection.socket not in server.clients


def assert_frames_match(where: str, actual: list[str], expected: list[Any]) -> None:
    """Compare frames byte-for-byte, except acknowledgement ``events`` sets."""
    assert len(actual) == len(expected), f"{where}: frames {actual!r}"
    for index, (frame, want) in enumerate(zip(actual, expected, strict=True)):
        if isinstance(want, str):
            assert frame.encode("utf-8") == want.encode("utf-8"), f"{where}[{index}]"
            continue
        ack = json.loads(frame)
        assert ack["type"] in ACK_TYPES, f"{where}[{index}]: {frame!r}"
        assert set(ack) == {"type", "events"}, f"{where}[{index}]: {frame!r}"
        assert len(ack["events"]) == len(set(ack["events"])), f"{where}[{index}]"
        normalized = {"type": ack["type"], "events": sorted(ack["events"])}
        assert normalized == want["ack"], f"{where}[{index}]"


def _handler_exceptions(records: list[logging.LogRecord]) -> list[str]:
    return [
        record.exc_info[0].__name__
        for record in records
        if record.name == SERVER_LOGGER
        and record.getMessage().startswith(HANDLER_ERROR_LOG)
        and record.exc_info is not None
        and record.exc_info[0] is not None
    ]


async def replay_case(case: dict[str, Any], caplog: pytest.LogCaptureFixture) -> int:
    """Run every step of one case against a fresh server; return the steps replayed."""
    server = _server()
    connections: dict[str, _Connection] = {}
    replayed = 0
    try:
        for name, spec in case["clients"].items():
            connections[name] = await _connect(server, name, spec)
        for index, step in enumerate(case["steps"]):
            where = f"{case['name']} step {index}"
            caplog.clear()
            await _run_step(server, connections, step, where)
            assert _handler_exceptions(caplog.records) == (
                [step["handler_exception"]] if "handler_exception" in step else []
            ), where
            replayed += 1
        return replayed
    finally:
        for connection in connections.values():
            await _disconnect(server, connection)


async def _run_step(
    server: WebSocketServer,
    connections: dict[str, _Connection],
    step: dict[str, Any],
    where: str,
) -> None:
    expected: dict[str, list[Any]]
    op = step["op"]
    if op == "send":
        sender = connections[step["client"]]
        await sender.wire.deliver(step["text"])
        expected = {name: [] for name in connections}
        expected[sender.name] = step["replies"]
        assert _subscription_state(sender.socket) == step["subscriptions"], where
    elif op == "broadcast":
        await server.broadcast(step["message"], frames=step.get("frames", ()))
        expected = step["deliveries"]
    elif op == "direct":
        # Handlers reply to their own connection with websocket.send(json_dumps(...)).
        await connections[step["client"]].socket.send(json_dumps(step["message"]))
        expected = step["deliveries"]
    else:
        raise CorpusError(f"{where}: unknown op {op!r}")
    assert set(expected) == set(connections), f"{where}: deliveries must name every client"
    for name, connection in connections.items():
        assert_frames_match(f"{where} client {name}", connection.take(), expected[name])


@pytest.mark.asyncio
@pytest.mark.parametrize("case_name", _declared_case_names())
async def test_python_replays_envelope_case(
    case_name: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger=SERVER_LOGGER)
    case = load_corpus(CORPUS_DIR)[case_name]
    assert await replay_case(case, caplog) == len(case["steps"]) > 0


def _write_corpus(directory: Path, manifest: dict[str, Any], cases: dict[str, Any]) -> None:
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "cases.json").write_text(json.dumps(cases), encoding="utf-8")


def test_manifest_is_complete(tmp_path: Path) -> None:
    corpus = load_corpus(CORPUS_DIR)
    assert list(corpus) == _declared_case_names()
    for case in corpus.values():
        assert case["steps"], case["name"]
        assert case["description"], case["name"]

    manifest = {"schema_version": SCHEMA_VERSION, "cases_file": "cases.json", "cases": ["a"]}
    case = {"name": "a", "description": "d", "clients": {}, "steps": []}
    drifts = {
        "unknown_manifest_version": (
            {**manifest, "schema_version": SCHEMA_VERSION + 1},
            {"schema_version": SCHEMA_VERSION, "cases": [case]},
            "manifest schema_version",
        ),
        "unknown_cases_version": (
            manifest,
            {"schema_version": SCHEMA_VERSION + 1, "cases": [case]},
            "cases schema_version",
        ),
        "missing_case": (
            manifest,
            {"schema_version": SCHEMA_VERSION, "cases": []},
            r"missing=\['a'\]",
        ),
        "unlisted_case": (
            manifest,
            {"schema_version": SCHEMA_VERSION, "cases": [case, {**case, "name": "b"}]},
            r"unlisted=\['b'\]",
        ),
        "duplicate_declaration": (
            {**manifest, "cases": ["a", "a"]},
            {"schema_version": SCHEMA_VERSION, "cases": [case]},
            "declares a case more than once",
        ),
        "duplicate_definition": (
            manifest,
            {"schema_version": SCHEMA_VERSION, "cases": [case, case]},
            "defined more than once",
        ),
    }
    for label, (drift_manifest, drift_cases, message) in drifts.items():
        directory = tmp_path / label
        _write_corpus(directory, drift_manifest, drift_cases)
        with pytest.raises(CorpusError, match=message):
            load_corpus(directory)


@pytest.mark.asyncio
async def test_frame_bytes_and_direct_delivery() -> None:
    corpus = load_corpus(CORPUS_DIR)
    steps = [step for case in corpus.values() for step in case["steps"]]
    fragmented = [step for step in steps if step["op"] == "broadcast" and step.get("frames")]
    assert fragmented, "the corpus must carry fragmented broadcasts"
    for step in fragmented:
        message_type = step["message"]["type"]
        assert all(frame["type"] != message_type for frame in step["frames"])
    assert any(step["op"] == "direct" for step in steps), "the corpus must carry direct replies"

    server = _server()
    connection = await _connect(server, "absent", {"entry": "direct", "initial_state": "absent"})
    try:
        message = {"type": "session_event", "event": "updated", "note": "café"}
        await server.broadcast(message)
        assert connection.take() == []

        await connection.socket.send(json_dumps(message))
        exact = '{"type": "session_event", "event": "updated", "note": "caf\\u00e9"}'
        frames = connection.take()
        assert_frames_match("direct", frames, [exact])
        # Equal JSON with different bytes must not pass the frame comparison.
        respaced = json.dumps(message, separators=(",", ":"))
        assert json.loads(respaced) == json.loads(exact)
        with pytest.raises(AssertionError):
            assert_frames_match("respaced", frames, [respaced])
    finally:
        await _disconnect(server, connection)
