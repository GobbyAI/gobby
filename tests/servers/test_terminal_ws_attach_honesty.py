"""Proxy attach reports typed failure instead of a silent success."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, cast
from unittest.mock import MagicMock

import pytest

from gobby.servers.websocket import terminal_ws
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import AttachLocator, HostEpochMismatchError, TerminalManager
from gobby.terminals import TerminalRuntime, TerminalRuntimeRegistry
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.write_coordinator import WriteCoordinator
from tests.servers.test_terminal_ws_lease import _live_row, _send, _ws_server
from tests.servers.test_tmux_mixin import MockWebSocket

pytestmark = pytest.mark.unit

_Kind = Literal[
    "no_runtime",
    "no_opener",
    "row_exited",
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
        MagicMock(),
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
    elif kind in {"row_exited", "locator_raises", "locator_epoch_stale", "locator_invalid"}:
        server.open_proxy_frame = _unused_opener
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "code", "reason"),
    [
        ("no_runtime", "runtime_unavailable", "no terminal runtime for backend"),
        ("no_opener", "proxy_unavailable", "proxy frame opener is not available"),
        (
            "row_exited",
            "terminal_exited",
            "terminal row is exited or orphaned; nothing to attach",
        ),
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
    if kind == "row_exited":
        assert TerminalManager(temp_db).mark_exited(terminal_id) is not None
    server = _ws_server()
    frame = _configure(server, temp_db, kind)
    ws = MockWebSocket()
    server.clients[ws] = {"subscriptions": {"*"}}
    with caplog.at_level(logging.WARNING, logger="gobby.servers.websocket.terminal_ws"):
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
    assert any(
        record.levelno == logging.WARNING
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
        MagicMock(),
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
        MagicMock(),
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
        assert result["reason"] == "terminal host has not finished starting"
