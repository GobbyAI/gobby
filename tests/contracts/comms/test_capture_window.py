"""Contracts for a provider request that is in flight while a capture window drains.

A request begins when the edge has its first byte. One that begins inside a capture window makes
that window's capture non-empty, as a pending hit until it completes, and never appears in a
later window. A tunnel that carries no request byte registers nothing. A case opens each edge's
window once, before any capture view of it exists, and every view of that edge shares the window,
so a request that arrives between two views stays in the case. Each test drives the real edge, the
recorder's real drain and, where a case is named, the replay comparison or the recorder's per-case
run. Every held peer waits on an event the test sets, and the drain bounds are shortened so no
test waits for the recorder's five second cap.
"""

from __future__ import annotations

import copy
import json
import queue
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.parity_waivers import DIFF, PASSED
from tests.contracts import corpus_v3, recorder_v3, recorder_v3_support
from tests.contracts.comms.fake_edge import (
    DECODE_ERROR_KEY,
    PENDING_KEY,
    UNKNOWN_FIELD,
    FakeEdge,
    empty_semantic,
)
from tests.contracts.http_corpus import classify_replay
from tests.contracts.normalizers_v3 import Window, normalize_captures
from tests.contracts.recorder_v3_support import ProviderEdgeCapture, RecorderError

# The bound on every event wait and on every peer socket operation.
GATE_SECONDS = 10.0
SHORT_QUIET_SECONDS = 0.1
# A cap for the tests that expect a drain to reach it with a request still pending.
SHORT_CAP_SECONDS = 0.6
# A cap far above the quiet period, for the tests that expect a drain to end before it.
LONG_CAP_SECONDS = 4.0
SLACK_HOST = "slack.com"
UPLOAD_HOST = "upload.corpus.test"
REFUSED_SEND_CASE = "comms_send_missing_channel"
POSITIVE_SEND_CASE = "comms_send"
SEND_PATH = "/api/chat.postMessage"
AUTH_PATH = "/api/auth.test"
SEND_BODY = json.dumps({"channel": "C0CORPUS0001", "text": "corpus message one"}).encode()
EMPTY_DELTA: dict[str, list[Any]] = {"added": [], "removed": [], "changed": []}
REPLAY_WINDOW = Window(start=datetime.now(UTC), end=datetime.now(UTC))
CONTENT_LENGTH = re.compile(rb"Content-Length: (\d+)", re.IGNORECASE)


def _post(host: str, path: str, body: bytes, *, close: bool) -> tuple[bytes, bytes]:
    """A JSON POST split after its Host header: the bytes that begin it and the rest."""
    begin = f"POST {path} HTTP/1.1\r\nHost: {host}\r\n".encode("latin-1")
    connection = "Connection: close\r\n" if close else ""
    rest = (
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n{connection}\r\n"
    ).encode("latin-1") + body
    return begin, rest


SEND_BEGIN, SEND_REST = _post(SLACK_HOST, SEND_PATH, SEND_BODY, close=True)
UPLOAD_BEGIN, UPLOAD_REST = _post(UPLOAD_HOST, "/upload/corpus-1", b"", close=True)


def _pending(host: str) -> dict[str, Any]:
    """The hit a request registers at its first byte, before its method or path is read."""
    return {
        "host": host,
        "method": UNKNOWN_FIELD,
        "path": UNKNOWN_FIELD,
        "semantic": empty_semantic(),
        PENDING_KEY: True,
    }


SEND_HIT = {
    "host": SLACK_HOST,
    "method": "POST",
    "path": SEND_PATH,
    "semantic": {
        **empty_semantic(),
        "destination": "C0CORPUS0001",
        "text": "corpus message one",
    },
}
AUTH_HIT = {"host": SLACK_HOST, "method": "POST", "path": AUTH_PATH, "semantic": empty_semantic()}

# One step of a held peer: it acts on the tunnel and may keep what it reads.
Step = Callable[[ssl.SSLSocket, bytearray], None]


def _send(data: bytes) -> Step:
    def step(tls: ssl.SSLSocket, _received: bytearray) -> None:
        tls.sendall(data)

    return step


def _signal(gate: threading.Event) -> Step:
    def step(_tls: ssl.SSLSocket, _received: bytearray) -> None:
        gate.set()

    return step


def _wait(gate: threading.Event) -> Step:
    def step(_tls: ssl.SSLSocket, _received: bytearray) -> None:
        if not gate.wait(GATE_SECONDS):
            raise TimeoutError("the test never released the held peer")

    return step


def _read_reply(tls: ssl.SSLSocket, received: bytearray) -> None:
    """Read one whole reply on a tunnel the edge keeps open."""
    while b"\r\n\r\n" not in received:
        chunk = tls.recv(4096)
        if not chunk:
            raise ConnectionError("the edge closed the tunnel before its reply")
        received.extend(chunk)
    head, _, body = bytes(received).partition(b"\r\n\r\n")
    declared = CONTENT_LENGTH.search(head)
    length = int(declared.group(1)) if declared is not None else 0
    while len(body) < length:
        chunk = tls.recv(4096)
        if not chunk:
            raise ConnectionError("the edge closed the tunnel inside its reply")
        body += chunk


def _read_to_eof(tls: ssl.SSLSocket, received: bytearray) -> None:
    while chunk := tls.recv(4096):
        received.extend(chunk)


def _fin(tls: ssl.SSLSocket, _received: bytearray) -> None:
    """End the peer's side of the tunnel, so the edge reads the end of the stream."""
    tls.shutdown(socket.SHUT_WR)


class _Peer:
    """A client tunnel to one provider host that runs its steps in order on its own thread."""

    def __init__(self, edge: FakeEdge, host: str, steps: tuple[Step, ...]) -> None:
        self._edge = edge
        self._host = host
        self._steps = steps
        self.received = bytearray()
        self.errors: list[BaseException] = []
        self.finished = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        context = ssl.create_default_context(cafile=str(self._edge.ca_path()))
        connect = f"CONNECT {self._host}:443 HTTP/1.1\r\nHost: {self._host}:443\r\n\r\n"
        try:
            with socket.create_connection(self._edge.address, timeout=GATE_SECONDS) as conn:
                conn.sendall(connect.encode("latin-1"))
                established = b""
                while b"\r\n\r\n" not in established:
                    chunk = conn.recv(4096)
                    if not chunk:
                        raise ConnectionError("the edge closed the CONNECT before answering")
                    established += chunk
                with context.wrap_socket(conn, server_hostname=self._host) as tls:
                    for step in self._steps:
                        step(tls, self.received)
        except (OSError, ssl.SSLError) as error:
            self.errors.append(error)
        finally:
            self.finished.set()

    def join(self) -> None:
        self._thread.join(GATE_SECONDS)


class _ReleasingEdge(FakeEdge):
    """An edge that sets an armed gate at the first log read past a delay after arming.

    A drain reads the log on every poll, so the gate opens while that drain is running.
    """

    def __init__(self, root: Path, *, on_request_begin: Callable[[str], None]) -> None:
        super().__init__(root, on_request_begin=on_request_begin)
        self._armed: tuple[threading.Event, float] | None = None

    def arm(self, gate: threading.Event, delay_seconds: float) -> None:
        self._armed = (gate, time.monotonic() + delay_seconds)

    def hits(self) -> list[dict[str, Any]]:
        if self._armed is not None and time.monotonic() > self._armed[1]:
            self._armed[0].set()
        return super().hits()


class _Harness:
    """One started edge, the hosts whose requests began at it, and the peers a test holds."""

    def __init__(self, root: Path) -> None:
        self._begun: queue.Queue[str] = queue.Queue()
        self.edge = _ReleasingEdge(root, on_request_begin=self._begun.put)
        self._gates: list[threading.Event] = []
        self._peers: list[_Peer] = []

    def gate(self) -> threading.Event:
        gate = threading.Event()
        self._gates.append(gate)
        return gate

    def peer(self, host: str, *steps: Step) -> _Peer:
        peer = _Peer(self.edge, host, steps)
        self._peers.append(peer)
        return peer

    def next_begin(self) -> str | None:
        """The host of the next request whose first byte the edge registered, if one arrives."""
        try:
            return self._begun.get(timeout=GATE_SECONDS)
        except queue.Empty:
            return None

    def close(self) -> None:
        for gate in self._gates:
            gate.set()
        for peer in self._peers:
            peer.join()
        self.edge.stop()


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[_Harness]:
    held = _Harness(tmp_path)
    held.edge.start()
    try:
        yield held
    finally:
        held.close()


def _bound_drain(monkeypatch: pytest.MonkeyPatch, cap_seconds: float) -> None:
    monkeypatch.setattr(recorder_v3_support, "CAPTURE_QUIET_SECONDS", SHORT_QUIET_SECONDS)
    monkeypatch.setattr(recorder_v3_support, "CAPTURE_CAP_SECONDS", cap_seconds)


def _case(name: str) -> dict[str, Any]:
    matches = [case for case in corpus_v3.load_cases(corpus_v3.V3_DIR) if case["name"] == name]
    assert len(matches) == 1, f"expected one v3 case named {name!r}, found {len(matches)}"
    return matches[0]


def _hosts(case: dict[str, Any]) -> list[str]:
    effects = [e for e in case["side_effects"] if e["channel"] == "provider_edge"]
    assert len(effects) == 1, f"{case['name']} declares {len(effects)} provider_edge effects"
    hosts: list[str] = effects[0]["match"]["hosts"]
    return hosts


def _classify(case: dict[str, Any], drained: list[dict[str, Any]]) -> str:
    """Classify a replay that answered the recorded reply and wrote the recorded rows."""
    effect = case["side_effects"][0]
    observed = {
        "db_delta": [copy.deepcopy(entry["recorded"]) for entry in case["db_delta"]],
        "side_effects": [
            normalize_captures(drained, effect.get("normalizers", {}), window=REPLAY_WINDOW)
        ],
    }
    return classify_replay(case, copy.deepcopy(case["response"]), {}, observed=observed).outcome


@pytest.mark.integration
def test_a_request_in_flight_is_pending_in_its_window_and_absent_from_the_next(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bound_drain(monkeypatch, SHORT_CAP_SECONDS)
    case = _case(REFUSED_SEND_CASE)
    release = harness.gate()
    with ProviderEdgeCapture(harness.edge, _hosts(case)) as capture:
        peer = harness.peer(
            SLACK_HOST, _send(SEND_BEGIN), _wait(release), _send(SEND_REST), _read_to_eof
        )
        assert harness.next_begin() == SLACK_HOST, (
            "the edge did not register the request's first byte before the drain"
        )
        drained = capture.drain()
        outcome = _classify(case, drained)
    assert drained == [_pending(SLACK_HOST)], (
        f"the window of a request held in flight drained {drained}"
    )
    assert outcome == DIFF, f"a refused send with a request in flight classifies {outcome}"
    harness.edge.clear()
    with ProviderEdgeCapture(harness.edge, _hosts(case)) as later:
        release.set()
        assert peer.finished.wait(GATE_SECONDS), "the released peer never completed"
        assert bytes(peer.received).startswith(b"HTTP/1.1 200"), (
            f"the edge answered the completed request with {bytes(peer.received)[:40]!r}"
        )
        drained_later = later.drain()
    assert drained_later == [], f"the next window drained the earlier request: {drained_later}"
    assert harness.edge.hits() == [], f"the next window logs {harness.edge.hits()}"


@pytest.mark.integration
def test_a_completed_request_keeps_the_slot_it_began_in(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bound_drain(monkeypatch, LONG_CAP_SECONDS)
    release = harness.gate()
    auth_begin, auth_rest = _post(SLACK_HOST, AUTH_PATH, b"", close=True)
    with ProviderEdgeCapture(harness.edge, [SLACK_HOST]) as capture:
        harness.peer(SLACK_HOST, _send(SEND_BEGIN), _wait(release), _send(SEND_REST), _read_to_eof)
        assert harness.next_begin() == SLACK_HOST, "the earlier request never began"
        later = harness.peer(SLACK_HOST, _send(auth_begin + auth_rest), _read_to_eof)
        assert harness.next_begin() == SLACK_HOST, "the later request never began"
        assert later.finished.wait(GATE_SECONDS), "the later request never completed"
        # The earlier request completes while the drain runs, after its quiet period has passed.
        harness.edge.arm(release, SHORT_QUIET_SECONDS * 2)
        drained = capture.drain()
    assert drained == [SEND_HIT, AUTH_HIT], (
        f"two requests that began in order and finished in reverse drained {drained}"
    )


@pytest.mark.integration
def test_a_positive_case_with_a_request_pending_at_the_cap_is_a_difference(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bound_drain(monkeypatch, SHORT_CAP_SECONDS)
    case = _case(POSITIVE_SEND_CASE)
    release = harness.gate()
    with ProviderEdgeCapture(harness.edge, _hosts(case)) as capture:
        harness.peer(SLACK_HOST, _send(SEND_BEGIN), _wait(release), _send(SEND_REST), _read_to_eof)
        assert harness.next_begin() == SLACK_HOST, "the request never began"
        started = time.monotonic()
        drained = capture.drain()
        waited = time.monotonic() - started
    assert drained == [_pending(SLACK_HOST)], f"a request pending at the cap drained {drained}"
    assert waited >= SHORT_CAP_SECONDS, (
        f"the drain returned after {waited:.2f}s with a request pending, before the cap"
    )
    assert _classify(case, [SEND_HIT]) == PASSED, "the recorded send is a difference"
    outcome = _classify(case, drained)
    assert outcome == DIFF, f"a send still pending at the cap classifies {outcome}"


def _recorded_or_refusal(case: dict[str, Any], scratch: Path, edge: FakeEdge) -> Any:
    """The case the recorder writes from the observed run, or its refusal."""
    # The observed run is substituted, so no run database or daemon is reached.
    unreached = recorder_v3.RunDatabase(name="", url="", base_url="")
    try:
        return recorder_v3.record_template(
            unreached, None, case, operator={}, scratch=scratch, edge=edge
        )
    except RecorderError as error:
        return f"refused: {error}"


@pytest.mark.integration
def test_the_recorder_refuses_a_capture_with_a_pending_request(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _bound_drain(monkeypatch, SHORT_CAP_SECONDS)
    case = _case(REFUSED_SEND_CASE)
    release = harness.gate()
    with ProviderEdgeCapture(harness.edge, _hosts(case)) as capture:
        harness.peer(SLACK_HOST, _send(SEND_BEGIN), _wait(release), _send(SEND_REST), _read_to_eof)
        assert harness.next_begin() == SLACK_HOST, "the request never began"
        drained = capture.drain()
    observation = recorder_v3.Observation(
        response=copy.deepcopy(case["response"]),
        db_delta=[copy.deepcopy(EMPTY_DELTA)],
        side_effects=[normalize_captures(drained, {}, window=REPLAY_WINDOW)],
    )

    def observed(*_: Any, **__: Any) -> tuple[recorder_v3.Observation, Window]:
        return observation, REPLAY_WINDOW

    monkeypatch.setattr(recorder_v3, "_run_case", observed)
    result = _recorded_or_refusal(case, tmp_path, harness.edge)
    assert isinstance(result, str) and result.startswith("refused: "), (
        f"the recorder wrote the capture {drained}: {result}"
    )
    assert drained == [_pending(SLACK_HOST)], f"the refused capture is {drained}"


@pytest.mark.integration
def test_a_drained_capture_does_not_change_when_its_request_completes(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bound_drain(monkeypatch, SHORT_CAP_SECONDS)
    release = harness.gate()
    with ProviderEdgeCapture(harness.edge, [SLACK_HOST]) as capture:
        peer = harness.peer(
            SLACK_HOST, _send(SEND_BEGIN), _wait(release), _send(SEND_REST), _read_to_eof
        )
        assert harness.next_begin() == SLACK_HOST, "the request never began"
        drained = capture.drain()
        assert drained == [_pending(SLACK_HOST)], f"the held request drained {drained}"
        kept = copy.deepcopy(drained)
        release.set()
        assert peer.finished.wait(GATE_SECONDS), "the released peer never completed"
        live = capture.hits()
    assert drained == kept, f"the drained capture changed to {drained} after the request completed"
    assert live == [SEND_HIT], f"the open window's log holds {live} after the request completed"


@pytest.mark.integration
def test_a_pending_request_to_another_host_does_not_delay_or_enter_a_capture(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bound_drain(monkeypatch, LONG_CAP_SECONDS)
    release = harness.gate()
    with ProviderEdgeCapture(harness.edge, [SLACK_HOST]) as capture:
        harness.peer(
            UPLOAD_HOST, _send(UPLOAD_BEGIN), _wait(release), _send(UPLOAD_REST), _read_to_eof
        )
        assert harness.next_begin() == UPLOAD_HOST, "the request to the other host never began"
        logged = harness.edge.hits()
        started = time.monotonic()
        drained = capture.drain()
        waited = time.monotonic() - started
    assert logged == [_pending(UPLOAD_HOST)], f"the edge logged {logged} for the held request"
    assert drained == [], f"a capture of {SLACK_HOST} drained {drained}"
    assert waited < LONG_CAP_SECONDS, (
        f"a request pending at another host held the drain for {waited:.2f}s"
    )


@pytest.mark.integration
@pytest.mark.parametrize("tunnel", ["fresh", "reused"])
def test_a_tunnel_with_no_request_byte_leaves_the_capture_empty(
    tunnel: str, harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bound_drain(monkeypatch, SHORT_CAP_SECONDS)
    ready, release = harness.gate(), harness.gate()
    held = (_signal(ready), _wait(release), _send(SEND_BEGIN), _wait(harness.gate()))
    if tunnel == "reused":
        auth_begin, auth_rest = _post(SLACK_HOST, AUTH_PATH, b"", close=False)
        harness.peer(SLACK_HOST, _send(auth_begin + auth_rest), _read_reply, *held)
        assert harness.next_begin() == SLACK_HOST, "the earlier request on the tunnel never began"
    else:
        harness.peer(SLACK_HOST, *held)
    assert ready.wait(GATE_SECONDS), f"the {tunnel} tunnel never opened"
    harness.edge.clear()
    with ProviderEdgeCapture(harness.edge, [SLACK_HOST]) as capture:
        drained = capture.drain()
        release.set()
        assert harness.next_begin() == SLACK_HOST, "the first byte on the tunnel never began"
        live = capture.hits()
    assert drained == [], f"a {tunnel} tunnel with no request byte drained {drained}"
    assert live == [_pending(SLACK_HOST)], (
        f"the first request byte on the {tunnel} tunnel registered {live}"
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "begin",
    [
        pytest.param(b"POST /api/chat.post", id="inside-request-line"),
        pytest.param(SEND_BEGIN, id="inside-header-block"),
    ],
)
def test_a_peer_fin_inside_the_head_is_captured_with_the_head_marker(
    begin: bytes, harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bound_drain(monkeypatch, SHORT_CAP_SECONDS)
    with ProviderEdgeCapture(harness.edge, [SLACK_HOST]) as capture:
        peer = harness.peer(SLACK_HOST, _send(begin), _fin, _read_to_eof)
        assert harness.next_begin() == SLACK_HOST, "the request never began"
        assert peer.finished.wait(GATE_SECONDS), "the edge never closed the ended tunnel"
        drained = capture.drain()
    markers = [str(item.get(DECODE_ERROR_KEY, "")) for item in drained]
    assert len(markers) == 1 and markers[0].startswith("head: "), (
        f"a peer FIN inside the head drained {drained}"
    )
    signature = {key: value for key, value in drained[0].items() if key != DECODE_ERROR_KEY}
    assert signature == {
        "host": SLACK_HOST,
        "method": UNKNOWN_FIELD,
        "path": UNKNOWN_FIELD,
        "semantic": empty_semantic(),
    }, f"a peer FIN inside the head was captured as {drained[0]}"


def _drain_two_views_around_an_arrival(
    harness: _Harness, first_hosts: Sequence[str], second_hosts: Sequence[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drain two views of one edge after a send begins between their registrations.

    The case's window is opened once, before either view exists. The send's first byte is
    registered after the first view is entered and before the second is, and the send completes
    before either view drains.
    """
    release = harness.gate()
    harness.edge.clear()
    with ExitStack() as stack:
        first = stack.enter_context(ProviderEdgeCapture(harness.edge, first_hosts))
        peer = harness.peer(
            SLACK_HOST, _send(SEND_BEGIN), _wait(release), _send(SEND_REST), _read_to_eof
        )
        assert harness.next_begin() == SLACK_HOST, "the send never began between the two views"
        second = stack.enter_context(ProviderEdgeCapture(harness.edge, second_hosts))
        release.set()
        assert peer.finished.wait(GATE_SECONDS), "the released send never completed"
        return first.drain(), second.drain()


@pytest.mark.integration
def test_an_arrival_for_the_later_view_between_two_views_is_captured_by_it(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bound_drain(monkeypatch, LONG_CAP_SECONDS)
    first, second = _drain_two_views_around_an_arrival(harness, [UPLOAD_HOST], [SLACK_HOST])
    assert second == [SEND_HIT], (
        f"the later view of {SLACK_HOST} drained {second} for a send that began before it"
    )
    assert first == [], f"the earlier view of {UPLOAD_HOST} drained {first}"


@pytest.mark.integration
def test_an_arrival_for_the_earlier_view_between_two_views_is_captured_by_it(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bound_drain(monkeypatch, LONG_CAP_SECONDS)
    first, second = _drain_two_views_around_an_arrival(harness, [SLACK_HOST], [UPLOAD_HOST])
    assert first == [SEND_HIT], (
        f"the earlier view of {SLACK_HOST} drained {first} after a second view was registered"
    )
    assert second == [], f"the later view of {UPLOAD_HOST} drained {second}"


class _BoundaryEdge(FakeEdge):
    """An unstarted edge that appends ``clear`` to a shared event list on every clear."""

    def __init__(self, root: Path, events: list[str]) -> None:
        super().__init__(root)
        self._events = events

    def clear(self) -> None:
        self._events.append("clear")
        super().clear()


@pytest.mark.integration
def test_the_recorder_opens_an_edge_window_once_per_case_before_its_views(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _bound_drain(monkeypatch, LONG_CAP_SECONDS)
    events: list[str] = []

    class _RecordedView(ProviderEdgeCapture):
        def __init__(self, edge: Any, hosts: Sequence[str]) -> None:
            events.append("view")
            super().__init__(edge, hosts)

    def sent(*_: Any, **__: Any) -> Any:
        events.append("send")
        return {"ok": True}

    monkeypatch.setattr(recorder_v3, "ProviderEdgeCapture", _RecordedView)
    monkeypatch.setattr(recorder_v3, "_send", sent)
    case = {
        "name": "two_edge_views_one_case",
        "kind": "http",
        "response": {"ok": True},
        "side_effects": [
            {"channel": "provider_edge", "match": {"hosts": [SLACK_HOST]}},
            {"channel": "provider_edge", "match": {"hosts": [UPLOAD_HOST]}},
        ],
    }
    # No seed or db_delta is declared and the send is substituted, so no database or daemon
    # is reached.
    unreached = recorder_v3.RunDatabase(name="", url="", base_url="")
    daemon = SimpleNamespace(http_url="http://127.0.0.1:9", home=tmp_path)
    observation, _ = recorder_v3._run_case(
        unreached,
        daemon,
        case,
        operator={},
        scratch=tmp_path,
        edge=_BoundaryEdge(tmp_path, events),
    )
    assert events == ["clear", "view", "view", "send"], (
        f"a case with two views of one edge ran its boundary as {events}"
    )
    assert observation.side_effects == [[], []], (
        f"two views of an edge nothing reached drained {observation.side_effects}"
    )


@pytest.mark.integration
def test_a_clear_of_an_empty_log_still_ends_the_generation(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bound_drain(monkeypatch, SHORT_CAP_SECONDS)
    release = harness.gate()
    harness.edge.clear()
    peer = harness.peer(
        SLACK_HOST, _send(SEND_BEGIN), _wait(release), _send(SEND_REST), _read_to_eof
    )
    assert harness.next_begin() == SLACK_HOST, "the request never began"
    harness.edge.clear()
    emptied = harness.edge.hits()
    retired = harness.edge._generation
    harness.edge.clear()
    advanced = harness.edge._generation - retired
    release.set()
    assert peer.finished.wait(GATE_SECONDS), "the released request never completed"
    with ProviderEdgeCapture(harness.edge, [SLACK_HOST]) as window:
        drained = window.drain()
    assert emptied == [], f"the clear of a log with a request in flight left {emptied}"
    assert drained == [], f"a request begun before two clears completed into {drained}"
    assert harness.edge.hits() == [], f"the new window logs {harness.edge.hits()}"
    assert advanced == 1, f"a clear of the empty log advanced the generation by {advanced}, not one"
