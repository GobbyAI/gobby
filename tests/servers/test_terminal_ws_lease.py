"""Acceptance 2.5.7+ lease, paste, disconnect, and generation tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.servers.websocket.message_classes import _message_leaves_the_read_loop
from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import AttachLocator, TerminalManager, native_locator_key
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.input_grants import sync_host_input_grant
from gobby.terminals.leases import HolderChange, TerminalLeaseRegistry
from gobby.terminals.runtime import Delivered, TerminalRuntime, WriteOutcome
from gobby.terminals.write_coordinator import WriteCoordinator
from tests.servers.terminal_fakes import MockWebSocket
from tests.storage.test_terminals import LOCAL_MACHINE_ID, _create_pending, _manager

pytestmark = pytest.mark.unit

_HOST_EPOCH = "epoch-1"
_HOST_TERMINAL_ID = "host-web"
_HOST_SOCKET = "/private/tmp/gobby-test/gterm-frames.sock"


class _NativeRuntime:
    backend = "native"

    def __init__(self) -> None:
        self.inputs: list[bytes] = []

    async def attach_locator(self, _terminal: Any) -> AttachLocator:
        return AttachLocator(
            backend="native",
            frame_host_epoch=_HOST_EPOCH,
            host_socket=_HOST_SOCKET,
            host_terminal_id=_HOST_TERMINAL_ID,
        )

    async def write_input(self, terminal: Any, data: bytes) -> WriteOutcome:
        del terminal
        self.inputs.append(data)
        return Delivered()


class _GrantingRuntime(_NativeRuntime):
    """Records the host grant calls the lease observer makes: (terminal, holder|None)."""

    def __init__(self) -> None:
        super().__init__()
        self.grants: list[tuple[str, str | None]] = []

    async def grant_input(self, terminal: Any, attachment_id: str) -> None:
        self.grants.append((str(terminal.id), attachment_id))

    async def revoke_input(self, terminal: Any, attachment_id: str | None = None) -> None:
        self.grants.append((str(terminal.id), None))


class _RuntimeRegistry(TerminalRuntimeRegistry):
    def __init__(self, runtime: _NativeRuntime | None = None) -> None:
        super().__init__()
        self.runtime = runtime or _NativeRuntime()
        self.register(cast(TerminalRuntime, self.runtime))


class _PausedCloseFrame:
    def __init__(self) -> None:
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.read_forever = asyncio.Event()

    async def read_message(self) -> dict[str, Any]:
        await self.read_forever.wait()
        return {}

    async def read_relay_message(self) -> dict[str, Any]:
        return await self.read_message()

    async def close(self) -> None:
        self.close_started.set()
        await self.release_close.wait()


@pytest.fixture(autouse=True)
def _machine() -> Any:
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


def _configure(
    server: WebSocketServer,
    temp_db: HubDatabase,
    runtimes: Any | None = None,
) -> None:
    manager = TerminalManager(temp_db)
    runtimes = _RuntimeRegistry() if runtimes is None else runtimes
    leases = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    server.configure_terminals(
        manager,
        runtimes,
        lease_registry=leases,
        write_coordinator=WriteCoordinator(manager, runtimes, lease_registry=leases),
    )


def _live_row(temp_db: HubDatabase, sample_project: dict[str, Any]) -> str:
    """A live native row: the lease protocol under test is backend-neutral,
    and a tmux row takes its lease on attach through the tmux-client path."""
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"], backend="native")
    promoted = manager.promote_to_live(
        pending.id,
        locator={"host_terminal_id": _HOST_TERMINAL_ID},
        locator_key=native_locator_key(_HOST_EPOCH, _HOST_TERMINAL_ID),
        host_epoch=_HOST_EPOCH,
        session_name="web-sess",
    )
    assert promoted is not None
    return promoted.id


async def _send(server: WebSocketServer, ws: MockWebSocket, payload: dict[str, Any]) -> None:
    await server._handle_message(ws, json.dumps(payload))


@pytest.mark.asyncio
async def test_lease_request_result_and_lost_events(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    _configure(server, temp_db)
    observer = MockWebSocket()
    holder = MockWebSocket()
    server.clients[observer] = {"subscriptions": {"*"}}
    server.clients[holder] = {"subscriptions": {"*"}}
    await _send(
        server,
        observer,
        {
            "type": "terminal_attach",
            "request_id": "a1",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    attach = observer.messages_of_type("terminal_attach_result")[0]
    assert "mode" not in attach
    assert attach["frame_delivery"] in {"proxy", "direct"}
    attachment = attach["attachment_id"]
    await _send(
        server,
        observer,
        {
            "type": "terminal_take_control",
            "terminal_id": terminal_id,
            "attachment_id": attachment,
            "takeover": False,
        },
    )
    granted = observer.messages_of_type("terminal_control_result")[-1]
    assert granted["granted"] is True
    await _send(
        server,
        holder,
        {
            "type": "terminal_attach",
            "request_id": "a2",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    other = holder.messages_of_type("terminal_attach_result")[0]["attachment_id"]
    await _send(
        server,
        holder,
        {
            "type": "terminal_take_control",
            "terminal_id": terminal_id,
            "attachment_id": other,
            "takeover": False,
        },
    )
    refused = holder.messages_of_type("terminal_control_result")[-1]
    assert refused["reason"] == "held"
    await _send(
        server,
        holder,
        {
            "type": "terminal_take_control",
            "terminal_id": terminal_id,
            "attachment_id": other,
            "takeover": True,
        },
    )
    lost = observer.messages_of_type("terminal_lease_lost")
    assert lost
    await _send(
        server,
        holder,
        {
            "type": "terminal_input",
            "terminal_id": terminal_id,
            "attachment_id": attachment,
            "data": "x",
            "client_write_seq": 1,
        },
    )
    outcome = holder.messages_of_type("terminal_write_outcome")[-1]
    assert outcome["outcome"] == "refused"


@pytest.mark.asyncio
async def test_attach_result_supplies_attachment_identity(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    _configure(server, temp_db)
    ws = MockWebSocket()
    server.clients[ws] = {"subscriptions": {"*"}}
    await _send(
        server,
        ws,
        {"type": "terminal_attach", "request_id": "missing", "terminal_id": str(uuid4())},
    )
    error = [m for m in ws.all_messages() if m.get("type") in {"terminal_attach_result", "error"}]
    assert error
    await _send(
        server,
        ws,
        {
            "type": "terminal_attach",
            "request_id": "ok",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    result = ws.messages_of_type("terminal_attach_result")[-1]
    assert result["attachment_id"]
    assert "mode" not in result
    await _send(
        server,
        ws,
        {
            "type": "terminal_take_control",
            "terminal_id": terminal_id,
            "attachment_id": result["attachment_id"],
            "takeover": False,
        },
    )
    assert ws.messages_of_type("terminal_control_result")


async def test_attach_and_inventory_report_current_lease_holder(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    _configure(server, temp_db)
    holder = MockWebSocket()
    observer = MockWebSocket()
    server.clients[holder] = {"subscriptions": {"*"}}
    server.clients[observer] = {"subscriptions": {"*"}}
    await _send(
        server,
        holder,
        {
            "type": "terminal_attach",
            "request_id": "holder",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    holder_attachment = holder.messages_of_type("terminal_attach_result")[-1]["attachment_id"]
    await _send(
        server,
        holder,
        {
            "type": "terminal_take_control",
            "terminal_id": terminal_id,
            "attachment_id": holder_attachment,
            "takeover": False,
        },
    )
    await _send(
        server,
        observer,
        {
            "type": "terminal_attach",
            "request_id": "observer",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    expected = {
        "attachment_id": holder_attachment,
        "kind": "gclient",
        "session_ref": None,
    }
    attach_result = observer.messages_of_type("terminal_attach_result")[-1]
    assert attach_result["success"], attach_result
    assert attach_result["lease_holder"] == expected
    await _send(
        server,
        observer,
        {"type": "terminal_list", "request_id": "inventory", "project_id": sample_project["id"]},
    )
    page = observer.messages_of_type("terminal_list")[-1]
    item = next(item for item in page["items"] if item["terminal_id"] == terminal_id)
    assert item["lease_holder"] == expected


async def test_explicit_takeover_displaces_the_existing_holder() -> None:
    registry = TerminalLeaseRegistry()
    first = await registry.attach("terminal-agent", "direct")
    second = await registry.attach("terminal-agent", "direct")
    initial = await registry.take_control("terminal-agent", first.attachment_id, takeover=False)
    assert initial.granted is True
    refused = await registry.take_control("terminal-agent", second.attachment_id, takeover=False)
    assert refused.granted is False
    assert refused.reason == "held"
    taken = await registry.take_control("terminal-agent", second.attachment_id, takeover=True)
    assert taken.granted is True
    assert taken.displaced_attachment_id == first.attachment_id
    assert registry.holder("terminal-agent") == second.attachment_id


@pytest.mark.asyncio
async def test_direct_delivery_registers_without_frame_relay(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    _configure(server, temp_db)
    ws = MockWebSocket()
    server.clients[ws] = {"subscriptions": {"*"}}
    await _send(
        server,
        ws,
        {
            "type": "terminal_attach",
            "request_id": "d1",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    result = ws.messages_of_type("terminal_attach_result")[-1]
    assert result["frame_delivery"] == "direct"
    old = result["attachment_id"]
    await server._cleanup_terminal_client(ws)
    ws2 = MockWebSocket()
    server.clients[ws2] = {"subscriptions": {"*"}}
    await _send(
        server,
        ws2,
        {
            "type": "terminal_attach",
            "request_id": "d2",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    fresh = ws2.messages_of_type("terminal_attach_result")[-1]["attachment_id"]
    assert fresh != old
    await _send(
        server,
        ws2,
        {
            "type": "terminal_take_control",
            "terminal_id": terminal_id,
            "attachment_id": old,
            "takeover": False,
        },
    )
    assert ws2.messages_of_type("terminal_control_result")[-1]["reason"] == "stale_attachment"


@pytest.mark.asyncio
async def test_paste_is_lease_gated_and_size_capped(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    _configure(server, temp_db)
    ws = MockWebSocket()
    server.clients[ws] = {"subscriptions": {"*"}}
    await _send(
        server,
        ws,
        {
            "type": "terminal_attach",
            "request_id": "p",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    attachment = ws.messages_of_type("terminal_attach_result")[-1]["attachment_id"]
    await _send(
        server,
        ws,
        {
            "type": "terminal_paste",
            "terminal_id": terminal_id,
            "attachment_id": attachment,
            "text": "hi",
            "client_write_seq": 1,
        },
    )
    refused = ws.messages_of_type("terminal_write_outcome")[-1]
    assert refused["outcome"] == "refused"
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
    oversize = "x" * (1024 * 1024 + 1)
    await _send(
        server,
        ws,
        {
            "type": "terminal_paste",
            "terminal_id": terminal_id,
            "attachment_id": attachment,
            "text": oversize,
            "client_write_seq": 2,
        },
    )
    cap = ws.messages_of_type("terminal_write_outcome")[-1]
    assert cap["outcome"] == "refused"
    assert ws.closed is False


@pytest.mark.asyncio
async def test_disconnect_releases_direct_and_proxy_leases(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    _configure(server, temp_db)
    ws = MockWebSocket()
    server.clients[ws] = {"subscriptions": {"*"}}
    await _send(
        server,
        ws,
        {
            "type": "terminal_attach",
            "request_id": "x",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    attachment = ws.messages_of_type("terminal_attach_result")[-1]["attachment_id"]
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
    await server._cleanup_terminal_client(ws)
    ws2 = MockWebSocket()
    server.clients[ws2] = {"subscriptions": {"*"}}
    await _send(
        server,
        ws2,
        {
            "type": "terminal_attach",
            "request_id": "y",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    other = ws2.messages_of_type("terminal_attach_result")[-1]["attachment_id"]
    await _send(
        server,
        ws2,
        {
            "type": "terminal_take_control",
            "terminal_id": terminal_id,
            "attachment_id": other,
            "takeover": False,
        },
    )
    assert ws2.messages_of_type("terminal_control_result")[-1]["granted"] is True


@pytest.mark.asyncio
async def test_release_control_is_idempotent_and_races_takeover(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    _configure(server, temp_db)
    ws = MockWebSocket()
    server.clients[ws] = {"subscriptions": {"*"}}
    await _send(
        server,
        ws,
        {
            "type": "terminal_attach",
            "request_id": "r",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    attachment = ws.messages_of_type("terminal_attach_result")[-1]["attachment_id"]
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
    await _send(
        server,
        ws,
        {
            "type": "terminal_release_control",
            "terminal_id": terminal_id,
            "attachment_id": attachment,
        },
    )
    first = ws.messages_of_type("terminal_control_result")[-1]
    await _send(
        server,
        ws,
        {
            "type": "terminal_release_control",
            "terminal_id": terminal_id,
            "attachment_id": attachment,
        },
    )
    second = ws.messages_of_type("terminal_control_result")[-1]
    assert second["granted"] is False
    assert second["reason"] == "released"
    await _send(
        server,
        ws,
        {
            "type": "terminal_release_control",
            "terminal_id": terminal_id,
            "attachment_id": str(uuid4()),
        },
    )
    stale = ws.messages_of_type("terminal_control_result")[-1]
    assert stale["reason"] == "stale_attachment"
    del first


@pytest.mark.asyncio
async def test_terminal_focus_marks_the_holder_and_sends_nothing(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    _configure(server, temp_db)
    ws = MockWebSocket()
    server.clients[ws] = {"subscriptions": {"*"}}
    await _send(
        server,
        ws,
        {
            "type": "terminal_attach",
            "request_id": "r",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    attachment = ws.messages_of_type("terminal_attach_result")[-1]["attachment_id"]
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
    sent = len(ws.all_messages())
    focus = {"type": "terminal_focus", "terminal_id": terminal_id, "attachment_id": attachment}

    await _send(server, ws, {**focus, "focused": True})
    assert len(ws.all_messages()) == sent
    assert server._leases().focus_held(terminal_id) is True

    await _send(server, ws, {**focus, "focused": False})
    assert len(ws.all_messages()) == sent
    assert server._leases().focus_held(terminal_id) is False

    await _send(server, ws, {**focus, "attachment_id": str(uuid4()), "focused": True})
    assert len(ws.all_messages()) == sent

    assert _message_leaves_the_read_loop(json.dumps(focus)) is True


@pytest.mark.asyncio
async def test_lease_write_linearizes_with_takeover() -> None:
    registry = TerminalLeaseRegistry()
    socket_a, socket_b = object(), object()
    a = await registry.attach("t", frame_delivery="proxy", websocket=socket_a)
    b = await registry.attach("t", frame_delivery="proxy", websocket=socket_b)
    gen = (await registry.take_control("t", a.attachment_id, takeover=False)).lease_generation
    await registry.take_control("t", b.attachment_id, takeover=True)
    refused = registry.admit_write(
        "t",
        websocket=socket_a,
        attachment_id=a.attachment_id,
        expected_lease_generation=gen,
        seq=1,
        kind="input",
        payload=b"x",
    )
    assert refused.ok is False


@pytest.mark.asyncio
async def test_stale_lease_generation_is_ignored() -> None:
    registry = TerminalLeaseRegistry()
    a = await registry.attach("t", frame_delivery="proxy")
    first = await registry.take_control("t", a.attachment_id, takeover=False)
    b = await registry.attach("t", frame_delivery="proxy")
    second = await registry.take_control("t", b.attachment_id, takeover=True)
    assert second.lease_generation > first.lease_generation


@pytest.mark.asyncio
async def test_frame_and_lag_paths_finalize_the_lease() -> None:
    registry = TerminalLeaseRegistry()
    a = await registry.attach("t", frame_delivery="proxy")
    await registry.take_control("t", a.attachment_id, takeover=False)
    event = await registry.finalize(a.attachment_id, reason="proxy_lag")
    assert event is not None
    assert event.reason == "proxy_lag"
    again = await registry.take_control("t", a.attachment_id, takeover=False)
    assert again.reason == "stale_attachment"


@pytest.mark.asyncio
async def test_observer_finalization_keeps_holder_writable() -> None:
    registry = TerminalLeaseRegistry()
    holder_socket, observer_socket = object(), object()
    holder = await registry.attach("t", frame_delivery="proxy", websocket=holder_socket)
    observer = await registry.attach("t", frame_delivery="proxy", websocket=observer_socket)
    granted = await registry.take_control("t", holder.attachment_id, takeover=False)
    await registry.finalize(observer.attachment_id, reason="detach")
    assert registry.holder("t") == holder.attachment_id
    admitted = registry.admit_write(
        "t",
        websocket=holder_socket,
        attachment_id=holder.attachment_id,
        expected_lease_generation=granted.lease_generation,
        seq=1,
        kind="input",
        payload=b"x",
    )
    assert admitted.ok is True


@pytest.mark.asyncio
async def test_takeover_then_old_disconnect_keeps_new_holder_writable() -> None:
    registry = TerminalLeaseRegistry()
    old_socket, new_socket = object(), object()
    old = await registry.attach("t", frame_delivery="proxy", websocket=old_socket)
    await registry.take_control("t", old.attachment_id, takeover=False)
    new = await registry.attach("t", frame_delivery="proxy", websocket=new_socket)
    granted = await registry.take_control("t", new.attachment_id, takeover=True)
    await registry.finalize(old.attachment_id, reason="ws_loss")
    admitted = registry.admit_write(
        "t",
        websocket=new_socket,
        attachment_id=new.attachment_id,
        expected_lease_generation=granted.lease_generation,
        seq=1,
        kind="input",
        payload=b"x",
    )
    assert admitted.ok is True


@pytest.mark.asyncio
async def test_equal_generation_held_and_release_results_are_applied() -> None:
    registry = TerminalLeaseRegistry()
    holder = await registry.attach("t", frame_delivery="proxy")
    other = await registry.attach("t", frame_delivery="proxy")
    await registry.take_control("t", holder.attachment_id, takeover=False)
    held = await registry.take_control("t", other.attachment_id, takeover=False)
    assert held.reason == "held"
    released = await registry.release_control(holder.attachment_id)
    duplicate = await registry.release_control(holder.attachment_id)
    assert released.reason == "released" or released.granted is True
    assert duplicate.reason == "released"


@pytest.mark.asyncio
async def test_attachment_required_and_runtime_unavailable(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    _configure(server, temp_db, TerminalRuntimeRegistry())
    websocket = MockWebSocket()

    await server._handle_terminal_input(
        websocket,
        {
            "terminal_id": terminal_id,
            "data": "missing attachment",
            "client_write_seq": 1,
        },
    )
    missing = websocket.messages_of_type("terminal_write_outcome")[-1]
    assert (missing["outcome"], missing["reason"]) == (
        "refused",
        "attachment_required",
    )

    attachment = await server.lease_registry.attach(
        terminal_id,
        websocket=websocket,
        attachment_id="unavailable-runtime",
    )
    granted = await server.lease_registry.take_control(
        terminal_id,
        attachment.attachment_id,
    )
    assert granted.granted
    await server._handle_terminal_input(
        websocket,
        {
            "terminal_id": terminal_id,
            "attachment_id": attachment.attachment_id,
            "data": "no runtime",
            "client_write_seq": 2,
        },
    )
    unavailable = websocket.messages_of_type("terminal_write_outcome")[-1]
    assert (unavailable["outcome"], unavailable["reason"]) == (
        "refused",
        "runtime_unavailable",
    )


@pytest.mark.asyncio
async def test_finalize_revokes_authority_before_frame_close(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    locator = AttachLocator(
        backend="native",
        frame_host_epoch=_HOST_EPOCH,
        host_socket=_HOST_SOCKET,
        host_terminal_id=_HOST_TERMINAL_ID,
    )
    for cleanup_path in ("finalize_attachment", "socket_fail"):
        runtime = _NativeRuntime()
        server = _ws_server()
        _configure(server, temp_db, _RuntimeRegistry(runtime))
        monkeypatch.setattr(server, "_apply_terminal_sizing", AsyncMock())
        websocket = MockWebSocket()
        attachment_id = f"attachment-{cleanup_path}"
        await server.lease_registry.attach(
            terminal_id,
            attachment_id=attachment_id,
            frame_delivery="proxy",
            websocket=websocket,
        )
        granted = await server.lease_registry.take_control(terminal_id, attachment_id)
        assert granted.granted
        frame = _PausedCloseFrame()
        proxy = server._proxy()
        await proxy.start_proxy(
            websocket,
            terminal_id=terminal_id,
            attachment_id=attachment_id,
            locator=locator,
            frame=frame,
            encoding="json",
        )

        if cleanup_path == "finalize_attachment":
            cleanup = asyncio.create_task(proxy.finalize_attachment(attachment_id, "ws_close"))
        else:
            cleanup = asyncio.create_task(proxy._on_socket_fail(websocket, "ws_close"))
        await frame.close_started.wait()
        assert not cleanup.done()
        assert server.lease_registry.get(attachment_id) is None

        writer = MockWebSocket()
        await server._handle_terminal_input(
            writer,
            {
                "terminal_id": terminal_id,
                "attachment_id": attachment_id,
                "data": "racing write",
                "client_write_seq": 1,
            },
        )
        refused = writer.messages_of_type("terminal_write_outcome")[-1]
        assert (refused["outcome"], refused["reason"]) == (
            "refused",
            "stale_attachment",
        )
        assert runtime.inputs == []
        frame.release_close.set()
        await cleanup
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_direct_gclient_holder_is_granted_and_revoked_on_transitions(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    runtime = _GrantingRuntime()
    _configure(server, temp_db, _RuntimeRegistry(runtime))

    async def follow(change: HolderChange) -> bool | None:
        return await sync_host_input_grant(runtime, change.terminal, change.holder)

    server.lease_registry.set_holder_observer(follow)
    gclient = MockWebSocket()
    server.clients[gclient] = {"subscriptions": {"*"}}
    await _send(
        server,
        gclient,
        {
            "type": "terminal_attach",
            "request_id": "g",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    direct = gclient.messages_of_type("terminal_attach_result")[-1]["attachment_id"]
    take = {
        "type": "terminal_take_control",
        "terminal_id": terminal_id,
        "attachment_id": direct,
        "takeover": False,
    }
    await _send(server, gclient, take)
    granted = gclient.messages_of_type("terminal_control_result")[-1]
    assert granted["granted"] is True
    assert granted["host_input_granted"] is True
    assert runtime.grants == [(terminal_id, direct)]

    # A web viewer takes over through the proxy path: the grant is revoked.
    assert server.terminal_manager is not None
    row = server.terminal_manager.get(terminal_id)
    web = MockWebSocket()
    server.clients[web] = {"subscriptions": {"*"}}
    web_record = await server.lease_registry.attach(
        terminal_id, "proxy", websocket=web, viewer="web", terminal=row
    )
    await _send(
        server,
        web,
        {
            "type": "terminal_take_control",
            "terminal_id": terminal_id,
            "attachment_id": web_record.attachment_id,
            "takeover": True,
        },
    )
    takeover = web.messages_of_type("terminal_control_result")[-1]
    assert takeover["granted"] is True
    assert takeover["host_input_granted"] is None
    assert runtime.grants[-1] == (terminal_id, None)
    assert gclient.messages_of_type("terminal_lease_lost")

    # Releasing leaves nobody holding: revoked again, never granted.
    await _send(
        server,
        web,
        {
            "type": "terminal_release_control",
            "terminal_id": terminal_id,
            "attachment_id": web_record.attachment_id,
        },
    )
    released = web.messages_of_type("terminal_control_result")[-1]
    assert released["reason"] == "released"
    assert released["host_input_granted"] is None
    assert runtime.grants == [(terminal_id, direct), (terminal_id, None), (terminal_id, None)]

    # The direct viewer takes again, then loses its socket: grant, then revoke.
    await _send(server, gclient, take)
    assert gclient.messages_of_type("terminal_control_result")[-1]["host_input_granted"] is True
    await server._cleanup_terminal_client(gclient)
    assert runtime.grants[-2:] == [(terminal_id, direct), (terminal_id, None)]


def _foreign_message(operation: str, terminal_id: str, attachment_id: str) -> dict[str, Any]:
    fields: dict[str, dict[str, Any]] = {
        "take_control": {"type": "terminal_take_control", "takeover": True},
        "release_control": {"type": "terminal_release_control"},
        "focus": {"type": "terminal_focus", "focused": True},
        "detach": {"type": "terminal_detach", "request_id": "detach-foreign"},
        "resize": {"type": "terminal_resize", "rows": 30, "cols": 90},
        "viewport": {"type": "terminal_set_viewport", "rows": 20, "cols": 70},
        "scroll": {"type": "terminal_set_scroll_offset", "rows_from_live_edge": 5, "max_rows": 10},
    }
    return {**fields[operation], "terminal_id": terminal_id, "attachment_id": attachment_id}


def _owner_state(server: WebSocketServer, terminal_id: str, attachment_id: str) -> tuple[Any, ...]:
    registry = server._leases()
    record = registry.get(attachment_id)
    return (
        registry.holder(terminal_id),
        registry.generation(terminal_id),
        registry.focus_held(terminal_id),
        None
        if record is None
        else (
            record.finalized,
            record.focused,
            record.geometry,
            record.resize_seq,
            record.viewport,
            record.scroll_offset,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["take_control", "release_control", "focus", "detach", "resize", "viewport", "scroll"],
)
async def test_foreign_attachment_operations_do_not_change_owner_state(
    temp_db: HubDatabase, sample_project: dict[str, Any], operation: str
) -> None:
    """A socket that did not create the attachment cannot change its state, whatever the id."""
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    _configure(server, temp_db)
    owner = MockWebSocket()
    foreign = MockWebSocket()
    server.clients[owner] = {"subscriptions": {"*"}}
    server.clients[foreign] = {"subscriptions": {"*"}}
    await _send(
        server,
        owner,
        {
            "type": "terminal_attach",
            "request_id": "r",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    attachment = owner.messages_of_type("terminal_attach_result")[-1]["attachment_id"]
    if operation != "take_control":
        # Every operation but take_control acts on the holder;
        # take_control acts on an idle attachment.
        await _send(
            server,
            owner,
            {
                "type": "terminal_take_control",
                "terminal_id": terminal_id,
                "attachment_id": attachment,
                "takeover": False,
            },
        )
        assert _owner_state(server, terminal_id, attachment)[0] == attachment
    before = _owner_state(server, terminal_id, attachment)

    await _send(server, foreign, _foreign_message(operation, terminal_id, attachment))

    assert _owner_state(server, terminal_id, attachment) == before


@pytest.mark.asyncio
async def test_foreign_theme_is_refused_as_stale_attachment(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """A socket that did not create the attachment gets stale_attachment for its theme."""
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    _configure(server, temp_db)
    owner = MockWebSocket()
    foreign = MockWebSocket()
    server.clients[owner] = {"subscriptions": {"*"}}
    server.clients[foreign] = {"subscriptions": {"*"}}
    await _send(
        server,
        owner,
        {
            "type": "terminal_attach",
            "request_id": "r",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    attachment = owner.messages_of_type("terminal_attach_result")[-1]["attachment_id"]

    await _send(
        server,
        foreign,
        {
            "type": "terminal_set_theme",
            "terminal_id": terminal_id,
            "attachment_id": attachment,
            "theme": {"foreground": "#ffffff", "background": "#000000", "palette": []},
        },
    )

    assert foreign.messages_of_type("terminal_error")[-1]["code"] == "stale_attachment"


@pytest.mark.asyncio
async def test_own_finalized_attachment_detaches_cleanly(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """The client keeps a server-finalized id and detaches it before attaching the next terminal.

    The web client starts the next attach only after a successful detach, so a
    connection's own finalized attachment must answer success without a second
    lifecycle event.
    """
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    _configure(server, temp_db)
    websocket = MockWebSocket()
    server.clients[websocket] = {"subscriptions": {"*"}}
    await _send(
        server,
        websocket,
        {
            "type": "terminal_attach",
            "request_id": "attach",
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
        },
    )
    attachment = websocket.messages_of_type("terminal_attach_result")[-1]["attachment_id"]
    registry = server._leases()
    event = await registry.finalize(attachment, "host_loss")
    assert event is not None

    async def publish(stamped: dict[str, Any]) -> None:
        await server._send_json(websocket, stamped)

    await registry.publish_lifecycle(
        {
            "type": "terminal_attachment_finalized",
            "terminal_id": event.terminal_id,
            "attachment_id": event.attachment_id,
            "reason": event.reason,
            "lease_generation": event.lease_generation,
        },
        publish,
    )
    assert len(websocket.messages_of_type("terminal_attachment_finalized")) == 1

    await _send(
        server,
        websocket,
        {
            "type": "terminal_detach",
            "request_id": "detach",
            "terminal_id": terminal_id,
            "attachment_id": attachment,
        },
    )

    detach = websocket.messages_of_type("terminal_detach_result")[-1]
    assert detach["success"] is True
    assert detach["request_id"] == "detach"
    assert len(websocket.messages_of_type("terminal_attachment_finalized")) == 1
    await registry.shutdown_lifecycle_publication()
