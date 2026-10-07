"""Proxy attach reports typed failure instead of a silent success."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest

from gobby.servers.websocket import terminal_ws
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import (
    AttachLocator,
    HostEpochMismatchError,
    TerminalManager,
    tmux_locator_key,
)
from gobby.terminals import TerminalRuntime, TerminalRuntimeRegistry
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.write_coordinator import WriteCoordinator
from tests.servers.terminal_fakes import MockWebSocket
from tests.servers.test_terminal_ws_lease import _live_row, _send, _ws_server
from tests.storage.test_terminals import _create_pending

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["direct", "proxy"])
async def test_attach_before_placed_spawn_commit_is_not_reported_stale(
    delivery: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Probe the actual placed-spawn row at bind, prepare, commit and promotion."""
    from gobby.agents.spawn_executor_runtime import _runtime_spawn
    from tests.agents.test_native_spawn import (
        ObservingHost,
        RecordingFrameClient,
        _native_request,
        _plan,
    )

    server = _ws_server()
    ws = MockWebSocket()
    server.clients[ws] = {"subscriptions": {"*"}}
    observations: list[tuple[str, str, str | None]] = []
    replies: list[dict[str, Any]] = []

    async def binder(terminal_id: str) -> None:
        row = store.get(terminal_id)
        assert row is not None
        observations.append(("bind", row.state, row.host_epoch))
        await _send(
            server,
            ws,
            {
                "type": "terminal_attach",
                "terminal_id": terminal_id,
                "frame_delivery": delivery,
            },
        )
        replies.append(ws.messages_of_type("terminal_attach_result")[-1])

    class ProbeHost(ObservingHost):
        async def spawn(self, **fields: Any) -> dict[str, Any]:
            row = store.get(str(fields["terminal_id"]))
            assert row is not None
            observations.append(("prepare", row.state, row.host_epoch))
            return await super().spawn(**fields)

        async def spawn_commit(
            self, terminal_id: str, spawn_key: str, commit_deadline_ms: int
        ) -> None:
            row = store.get(terminal_id)
            assert row is not None
            observations.append(("commit", row.state, row.host_epoch))
            await super().spawn_commit(terminal_id, spawn_key, commit_deadline_ms)

    host = ProbeHost()
    request, runtime, store = _native_request(
        host=host, frame=RecordingFrameClient(), placement_binder=binder
    )
    monkeypatch.setattr("gobby.agents.spawn_executor._persist_spawn_workspace", lambda *args: None)
    monkeypatch.setattr(runtime, "_socket_dir", lambda: tmp_path)
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    manager = cast(TerminalManager, store)
    leases = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    server.configure_terminals(
        manager,
        registry,
        lease_registry=leases,
        write_coordinator=WriteCoordinator(manager, registry, lease_registry=leases),
    )
    server.open_proxy_frame = _raising_opener
    result = await _runtime_spawn(request, _plan())
    assert result.success is True
    assert observations == [
        ("bind", "pending", None),
        ("prepare", "pending", None),
        ("commit", "pending", None),
    ]
    assert replies[0]["success"] is False
    assert replies[0]["code"] == "host_not_ready"
    assert result.terminal_id is not None
    row = store.get(result.terminal_id)
    assert row is not None
    assert (row.state, row.host_epoch) == ("live", host.host_epoch)
    await _send(
        server,
        ws,
        {
            "type": "terminal_attach",
            "terminal_id": row.id,
            "frame_delivery": "direct",
        },
    )
    attached = ws.messages_of_type("terminal_attach_result")[-1]
    assert attached["success"] is True
    assert attached["direct"]["host_epoch"] == host.host_epoch


_Kind = Literal[
    "no_runtime",
    "no_opener",
    "locator_raises",
    "locator_epoch_stale",
    "locator_invalid",
    "opener_raises",
    "opener_hangs",
    "frame_none",
    "frame_unusable",
    "start_proxy_raises",
    "start_proxy_hangs",
    "attach_raises",
]


class _LocatorRuntime:
    """Non-Mock runtime so _runtime_for does not treat it as missing."""

    backend = "native"

    def __init__(self, result: object | None = None, *, error: BaseException | None = None) -> None:
        self._result = result
        self._error = error

    async def attach_locator(self, row: object) -> object:
        del row
        if self._error is not None:
            raise self._error
        return self._result


def _valid_locator() -> AttachLocator:
    return AttachLocator(backend="native", frame_host_epoch="epoch-1", host_terminal_id="host-web")


async def _unused_opener(_locator: AttachLocator) -> object:
    raise AssertionError("open_proxy_frame should not run")


async def _raising_opener(_locator: AttachLocator) -> object:
    raise OSError("frame host down")


async def _hanging_opener(_locator: AttachLocator) -> object:
    """A host whose frame socket never accepts: the open outlives its budget."""
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


async def _none_opener(_locator: AttachLocator) -> object | None:
    return None


class _UnusableFrame:
    """Frame without read_message: the relay pump can never run it."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _ExplodingFrame:
    """Frame whose handshake blows up inside ProxyHub.start_proxy."""

    def __init__(self) -> None:
        self.closed = False

    async def read_message(self) -> dict[str, Any]:
        raise AssertionError("pump should never start")

    async def handshake(self, locator: AttachLocator, *, encoding: str) -> None:
        del locator, encoding
        raise OSError("handshake refused")

    async def close(self) -> None:
        self.closed = True


class _HangingFrame(_ExplodingFrame):
    """Frame whose handshake never answers: the start outlives its budget."""

    async def handshake(self, locator: AttachLocator, *, encoding: str) -> None:
        del locator, encoding
        await asyncio.Event().wait()


class _AttachExplodingFrame(_ExplodingFrame):
    """Frame whose handshake succeeds before attach_terminal blows up."""

    async def handshake(self, locator: AttachLocator, *, encoding: str) -> None:
        del locator, encoding

    async def attach_terminal(
        self, locator: AttachLocator, *, reservation_id: str | None = None
    ) -> None:
        del locator, reservation_id
        raise OSError("attach refused")


def _configure(
    server: Any, temp_db: HubDatabase, kind: _Kind
) -> _ExplodingFrame | _UnusableFrame | None:
    registry = TerminalRuntimeRegistry()
    if kind != "no_runtime":
        if kind == "locator_raises":
            runtime: _LocatorRuntime = _LocatorRuntime(error=RuntimeError("locator boom"))
        elif kind == "locator_epoch_stale":
            runtime = _LocatorRuntime(error=HostEpochMismatchError("stale host epoch"))
        elif kind == "locator_invalid":
            runtime = _LocatorRuntime(result={"not": "a locator"})
        else:
            runtime = _LocatorRuntime(result=_valid_locator())
        registry.register(cast(TerminalRuntime, runtime))
    manager = TerminalManager(temp_db)
    leases = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    server.configure_terminals(
        manager,
        registry,
        lease_registry=leases,
        write_coordinator=WriteCoordinator(manager, registry, lease_registry=leases),
    )
    if kind == "opener_raises":
        server.open_proxy_frame = _raising_opener
    elif kind == "opener_hangs":
        server.open_proxy_frame = _hanging_opener
    elif kind == "frame_none":
        server.open_proxy_frame = _none_opener
    elif kind in {"frame_unusable", "start_proxy_raises", "start_proxy_hangs", "attach_raises"}:
        if kind == "frame_unusable":
            frame: _ExplodingFrame | _UnusableFrame = _UnusableFrame()
        elif kind == "start_proxy_raises":
            frame = _ExplodingFrame()
        elif kind == "start_proxy_hangs":
            frame = _HangingFrame()
        else:
            frame = _AttachExplodingFrame()

        async def _frame_opener(_locator: AttachLocator) -> _ExplodingFrame | _UnusableFrame:
            return frame

        server.open_proxy_frame = _frame_opener
        return frame
    elif kind in {"locator_raises", "locator_epoch_stale", "locator_invalid"}:
        server.open_proxy_frame = _unused_opener
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "code", "reason"),
    [
        ("no_runtime", "runtime_unavailable", "no terminal runtime for backend"),
        ("no_opener", "proxy_unavailable", "proxy frame opener is not available"),
        ("locator_raises", "locator_failed", "attach_locator raised"),
        (
            "locator_epoch_stale",
            "host_epoch_stale",
            "terminal belongs to an earlier gterm host incarnation",
        ),
        (
            "locator_invalid",
            "locator_invalid",
            "attach_locator did not return an AttachLocator",
        ),
        ("opener_raises", "host_unavailable", "opening the proxy frame connection failed"),
        (
            "opener_hangs",
            "host_open_timeout",
            "opening the proxy frame connection did not finish in time",
        ),
        ("frame_none", "frame_invalid", "proxy frame opener returned an unusable frame"),
        ("frame_unusable", "frame_invalid", "proxy frame opener returned an unusable frame"),
        (
            "start_proxy_raises",
            "proxy_start_failed",
            "proxy frame handshake or relay start failed",
        ),
        (
            "start_proxy_hangs",
            "proxy_start_timeout",
            "proxy frame handshake did not finish in time",
        ),
        (
            "attach_raises",
            "proxy_start_failed",
            "proxy frame handshake or relay start failed",
        ),
    ],
)
async def test_proxy_attach_failures_are_typed_and_finalized(
    kind: _Kind,
    code: str,
    reason: str,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The hanging cases outlive their budgets; every other case finishes at
    # once, so a tiny budget changes nothing for them (#22544).
    monkeypatch.setattr(terminal_ws, "PROXY_FRAME_OPEN_SECONDS", 0.01)
    monkeypatch.setattr(terminal_ws, "PROXY_START_SECONDS", 0.01)
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    frame = _configure(server, temp_db, kind)
    ws = MockWebSocket()
    server.clients[ws] = {"subscriptions": {"*"}}
    with caplog.at_level(logging.DEBUG, logger="gobby.servers.websocket.terminal_ws"):
        await _send(
            server,
            ws,
            {
                "type": "terminal_attach",
                "request_id": "proxy-fail",
                "terminal_id": terminal_id,
                "frame_delivery": "proxy",
            },
        )
    result = ws.messages_of_type("terminal_attach_result")[-1]
    assert result["success"] is False
    assert result["code"] == code
    assert result["reason"] == reason
    assert result["terminal_id"] == terminal_id
    attachment = result["attachment_id"]
    assert attachment
    assert server.lease_registry.get(attachment) is None
    if frame is not None:
        assert frame.closed is True
    assert attachment not in server._proxy().attachments
    assert ws not in server._proxy().by_socket
    expected_level = logging.DEBUG if code == "terminal_exited" else logging.WARNING
    assert any(
        record.levelno == expected_level
        and code in record.getMessage()
        and terminal_id in record.getMessage()
        and reason in record.getMessage()
        for record in caplog.records
    )
    await _send(
        server,
        ws,
        {
            "type": "terminal_take_control",
            "terminal_id": terminal_id,
            "attachment_id": attachment,
            "takeover": False,
        },
    )
    control = ws.messages_of_type("terminal_control_result")[-1]
    assert control["granted"] is False
    assert control["reason"] == "stale_attachment"


@pytest.mark.asyncio
async def test_slow_failed_proxy_attach_logs_transport_phase(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    _configure(server, temp_db, "no_runtime")
    websocket = MockWebSocket()
    server.clients[websocket] = {"id": "client-1", "subscriptions": {"*"}}
    ticks = iter((0.0, 0.1, 0.2, 1.4, 1.6))
    monkeypatch.setattr(terminal_ws, "time", SimpleNamespace(monotonic=ticks.__next__))

    await _send(
        server,
        websocket,
        {
            "type": "terminal_attach",
            "request_id": "slow-proxy-failure",
            "terminal_id": terminal_id,
            "frame_delivery": "proxy",
        },
    )

    assert websocket.messages_of_type("terminal_attach_result")[-1]["code"] == "runtime_unavailable"
    assert (
        f"client_id=client-1 terminal_id={terminal_id} outcome=runtime_unavailable "
        "total_ms=1600.0 row_ms=100.0 lease_ms=100.0 transport_ms=1200.0 reply_ms=200.0"
    ) in caplog.text


class _StartupHost:
    """Host manager stand-in that records whether attach waited on startup."""

    def __init__(self, settled: bool) -> None:
        self._settled = settled
        self.waits: list[float] = []
        self.input_activity_sink: object | None = None

    def set_input_activity_sink(self, sink: object | None) -> None:
        self.input_activity_sink = sink

    async def wait_startup_settled(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return self._settled


class _AfterStartupRuntime(_LocatorRuntime):
    """Locator runtime that proves the host startup wait ran first."""

    def __init__(self, host: _StartupHost) -> None:
        super().__init__(result=_valid_locator())
        self._host = host
        self.resolved = 0

    async def attach_locator(self, row: object) -> object:
        assert self._host.waits, "attach_locator ran before the host startup wait"
        self.resolved += 1
        return await super().attach_locator(row)


@pytest.mark.asyncio
async def test_direct_attach_refuses_exited_row(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    manager = TerminalManager(temp_db)
    assert manager.mark_exited(terminal_id) is not None
    server = _ws_server()
    host = _StartupHost(settled=True)
    runtime = _AfterStartupRuntime(host)
    registry = TerminalRuntimeRegistry()
    registry.register(cast(TerminalRuntime, runtime))
    leases = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    server.configure_terminals(
        manager,
        registry,
        host_manager=host,
        lease_registry=leases,
        write_coordinator=WriteCoordinator(manager, registry, lease_registry=leases),
    )
    ws = MockWebSocket()
    server.clients[ws] = {"subscriptions": {"*"}}
    await _send(
        server,
        ws,
        {
            "type": "terminal_attach",
            "request_id": "direct-exited",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    result = ws.messages_of_type("terminal_attach_result")[-1]
    assert result["success"] is False
    assert result["code"] == "terminal_exited"
    assert result["reason"] == "terminal row is exited or orphaned; nothing to attach"
    assert host.waits == []
    assert runtime.resolved == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "code"),
    [
        ("missing", "terminal_gone"),
        ("exited", "terminal_exited"),
        ("orphaned", "terminal_orphaned"),
    ],
)
async def test_attach_fences_unavailable_rows_before_acquiring_a_lease(
    state: str, code: str, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager = TerminalManager(temp_db)
    terminal_id = str(uuid.uuid4())
    if state != "missing":
        terminal_id = _live_row(temp_db, sample_project)
        if state == "exited":
            assert manager.mark_exited(terminal_id) is not None
        else:
            assert manager.mark_orphaned(terminal_id) is not None
    reply, leases, runtime = await _attach_reply(manager, terminal_id)

    assert reply["success"] is False
    assert reply["code"] == code
    assert reply["reason"]
    assert "attachment_id" not in reply
    assert leases._attachments == {}
    assert runtime.resolved == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "live"])
async def test_attach_to_a_backend_without_a_runtime_releases_its_lease(
    state: str, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """A tmux row attaches like any live row; only a missing runtime refuses it."""
    manager = TerminalManager(temp_db)
    pending = _create_pending(manager, sample_project["id"], backend="tmux")
    if state == "live":
        socket_path = "/tmp/hand-started-tmux.sock"
        promoted = manager.promote_to_live(
            pending.id,
            locator={"socket_path": socket_path, "pane_id": "%1"},
            locator_key=tmux_locator_key(
                socket_path=socket_path,
                server_pid=100,
                server_start_time=200,
                pane_id="%1",
            ),
            session_name="hand-started",
        )
        assert promoted is not None
    reply, leases, runtime = await _attach_reply(manager, pending.id)

    assert reply["success"] is False
    assert reply["code"] == "runtime_unavailable"
    assert reply["reason"]
    assert leases.get(reply["attachment_id"]) is None
    assert runtime.resolved == 0


async def _attach_reply(
    manager: TerminalManager, terminal_id: str
) -> tuple[dict[str, Any], TerminalLeaseRegistry, _AfterStartupRuntime]:
    """Attach through a server whose only runtime is native."""
    server = _ws_server()
    runtime = _AfterStartupRuntime(_StartupHost(settled=True))
    registry = TerminalRuntimeRegistry()
    registry.register(cast(TerminalRuntime, runtime))
    leases = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    server.configure_terminals(
        manager,
        registry,
        lease_registry=leases,
        write_coordinator=WriteCoordinator(manager, registry, lease_registry=leases),
    )
    ws = MockWebSocket()
    server.clients[ws] = {"subscriptions": {"*"}}
    await _send(server, ws, {"type": "terminal_attach", "terminal_id": terminal_id})
    return ws.messages_of_type("terminal_attach_result")[-1], leases, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("settled", [True, False])
async def test_proxy_attach_waits_for_terminal_host_startup(
    settled: bool, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """A client reconnecting during daemon startup attaches once the gterm host
    has been adopted or spawned; a host that never settles is a typed refusal
    rather than an epoch mismatch against an unset host (#22002)."""
    from gobby.servers.websocket import terminal_ws

    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    host = _StartupHost(settled)
    runtime = _AfterStartupRuntime(host)
    registry = TerminalRuntimeRegistry()
    registry.register(cast(TerminalRuntime, runtime))
    manager = TerminalManager(temp_db)
    leases = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    server.configure_terminals(
        manager,
        registry,
        host_manager=host,
        lease_registry=leases,
        write_coordinator=WriteCoordinator(manager, registry, lease_registry=leases),
    )
    server.open_proxy_frame = _raising_opener
    ws = MockWebSocket()
    server.clients[ws] = {"subscriptions": {"*"}}
    await _send(
        server,
        ws,
        {
            "type": "terminal_attach",
            "request_id": "startup-wait",
            "terminal_id": terminal_id,
            "frame_delivery": "proxy",
        },
    )
    result = ws.messages_of_type("terminal_attach_result")[-1]
    assert result["success"] is False
    assert host.waits == [terminal_ws.HOST_STARTUP_ATTACH_WAIT_SECONDS]
    if settled:
        assert runtime.resolved == 1
        assert result["code"] == "host_unavailable"
    else:
        assert runtime.resolved == 0
        assert result["code"] == "host_not_ready"
        assert result["reason"] == "terminal host or terminal spawn is not ready"


class _ThreadRecordingManager(TerminalManager):
    """Records the thread each row read runs on."""

    def __init__(self, db: HubDatabase) -> None:
        super().__init__(db)
        self.get_threads: list[int] = []

    def get(self, terminal_id: str) -> Any:
        self.get_threads.append(threading.get_ident())
        return super().get(terminal_id)


@pytest.mark.asyncio
async def test_attach_resolves_the_row_on_the_db_executor(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """D1a.1: the attach row lookup runs on a worker thread, never the event loop."""
    terminal_id = _live_row(temp_db, sample_project)
    manager = _ThreadRecordingManager(temp_db)
    assert manager.mark_exited(terminal_id) is not None
    server = _ws_server()
    leases = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    registry = TerminalRuntimeRegistry()
    server.configure_terminals(
        manager,
        registry,
        lease_registry=leases,
        write_coordinator=WriteCoordinator(manager, registry, lease_registry=leases),
    )
    ws = MockWebSocket()
    server.clients[ws] = {"subscriptions": {"*"}}
    loop_thread = threading.get_ident()

    await _send(
        server,
        ws,
        {"type": "terminal_attach", "request_id": "executor-row", "terminal_id": terminal_id},
    )

    assert ws.messages_of_type("terminal_attach_result")[-1]["code"] == "terminal_exited"
    assert len(manager.get_threads) == 1
    assert manager.get_threads[0] != loop_thread
