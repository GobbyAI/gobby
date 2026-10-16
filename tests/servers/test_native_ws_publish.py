"""Native WS publication from Python to gdaemon (#24620, B1)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.websockets import WebSocket

import gobby.runner_backend as runner_backend
import gobby.servers.websocket.native_publish as native_publish
from gobby.config.bootstrap import BootstrapConfig
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.native_bridge import NativeBridgeAdapter, add_server_subscriptions
from gobby.servers.websocket.native_publish import (
    NativePublisher,
    native_publisher_from_environment,
)
from gobby.servers.websocket.server import WebSocketServer
from gobby.servers.websocket.session_control import SessionControlMixin
from gobby.utils.json_helpers import json_dumps

pytestmark = pytest.mark.unit

SECRET = "native-publish-test-front-door"
TOKEN = "backend-token-1"
BASE_URL = "http://127.0.0.1:60887"
GEN1 = f"{1:032x}"
GEN2 = f"{2:032x}"
CONFIG = WebSocketConfig(max_message_size=4096, ping_interval=7, ping_timeout=3)


class _Daemon:
    """gdaemon's private bind and publish endpoints, with a gate to stall the sender."""

    def __init__(self, log: list[str] | None = None) -> None:
        self.generations = [f"{n:032x}" for n in range(1, 10)]
        self.bind_status: list[int] = []
        self.binds: list[httpx.Request] = []
        self.bind_times: list[float] = []
        self.requests: list[dict[str, Any]] = []
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
            publisher.publish(second, frames=fragments),
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

        # The request bound is checked before anything is sent.
        assert native_publish.MAX_RETAINED_BYTES == 128 * 1024 * 1024
        monkeypatch.setattr(native_publish, "MAX_RETAINED_BYTES", 512)
        oversized = publisher.publish({"type": "agent_event", "blob": "y" * 1024})
        assert oversized.done()
        assert oversized.result() is None
        await _turns()
        assert all("y" * 1024 not in json.dumps(request) for request in daemon.requests)
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
    ) == (2.0, 5.0, 1.0)
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
    for fd, secret in [(None, None), ("9", None), (None, SECRET), ("", SECRET), ("9", "")]:
        for name, value in (("GOBBY_SUPERVISOR_FD", fd), ("GOBBY_FRONT_DOOR_SECRET", secret)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        assert native_publisher_from_environment(CONFIG, bootstrap) is None, (fd, secret)

    monkeypatch.setenv("GOBBY_SUPERVISOR_FD", "9")
    monkeypatch.setenv("GOBBY_FRONT_DOOR_SECRET", SECRET)
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
        assert (publisher.retained_bytes, publisher.retained_ops) == (3 * size, 3)
        [overflow] = await asyncio.gather(produce(3))
        assert overflow.done()
        assert overflow.result() is None
        assert all(future.done() and future.result() is None for future in futures)
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
