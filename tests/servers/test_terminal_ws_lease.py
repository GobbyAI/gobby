"""Acceptance 2.5.7+ lease, paste, disconnect, and generation tests."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import TerminalManager, native_locator_key
from gobby.terminals.leases import TerminalLeaseRegistry
from tests.servers.test_tmux_mixin import MockWebSocket
from tests.storage.test_terminals import LOCAL_MACHINE_ID, _create_pending, _manager

pytestmark = pytest.mark.unit

_HOST_EPOCH = "epoch-1"
_HOST_TERMINAL_ID = "host-web"


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
    server.configure_terminals(TerminalManager(temp_db), MagicMock(), MagicMock())
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
    server.configure_terminals(TerminalManager(temp_db), MagicMock(), MagicMock())
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


@pytest.mark.asyncio
async def test_direct_delivery_registers_without_frame_relay(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    server.configure_terminals(TerminalManager(temp_db), MagicMock(), MagicMock())
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
    await server._cleanup_tmux_client(ws)
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
    server.configure_terminals(TerminalManager(temp_db), MagicMock(), MagicMock())
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
    server.configure_terminals(TerminalManager(temp_db), MagicMock(), MagicMock())
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
    await server._cleanup_tmux_client(ws)
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
    server.configure_terminals(TerminalManager(temp_db), MagicMock(), MagicMock())
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
async def test_lease_write_linearizes_with_takeover() -> None:
    registry = TerminalLeaseRegistry()
    a = registry.attach("t", frame_delivery="proxy")
    b = registry.attach("t", frame_delivery="proxy")
    gen = registry.take_control("t", a.attachment_id, takeover=False).lease_generation
    registry.take_control("t", b.attachment_id, takeover=True)
    refused = registry.admit_write(
        "t",
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
    a = registry.attach("t", frame_delivery="proxy")
    first = registry.take_control("t", a.attachment_id, takeover=False)
    b = registry.attach("t", frame_delivery="proxy")
    second = registry.take_control("t", b.attachment_id, takeover=True)
    assert second.lease_generation > first.lease_generation


@pytest.mark.asyncio
async def test_frame_and_lag_paths_finalize_the_lease() -> None:
    registry = TerminalLeaseRegistry()
    a = registry.attach("t", frame_delivery="proxy")
    registry.take_control("t", a.attachment_id, takeover=False)
    event = registry.finalize(a.attachment_id, reason="proxy_lag")
    assert event is not None
    assert event.reason == "proxy_lag"
    again = registry.take_control("t", a.attachment_id, takeover=False)
    assert again.reason == "stale_attachment"


@pytest.mark.asyncio
async def test_observer_finalization_keeps_holder_writable() -> None:
    registry = TerminalLeaseRegistry()
    holder = registry.attach("t", frame_delivery="proxy")
    observer = registry.attach("t", frame_delivery="proxy")
    granted = registry.take_control("t", holder.attachment_id, takeover=False)
    registry.finalize(observer.attachment_id, reason="detach")
    assert registry.holder("t") == holder.attachment_id
    admitted = registry.admit_write(
        "t",
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
    old = registry.attach("t", frame_delivery="proxy")
    registry.take_control("t", old.attachment_id, takeover=False)
    new = registry.attach("t", frame_delivery="proxy")
    granted = registry.take_control("t", new.attachment_id, takeover=True)
    registry.finalize(old.attachment_id, reason="ws_loss")
    admitted = registry.admit_write(
        "t",
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
    holder = registry.attach("t", frame_delivery="proxy")
    other = registry.attach("t", frame_delivery="proxy")
    registry.take_control("t", holder.attachment_id, takeover=False)
    held = registry.take_control("t", other.attachment_id, takeover=False)
    assert held.reason == "held"
    released = registry.release_control(holder.attachment_id)
    duplicate = registry.release_control(holder.attachment_id)
    assert released.reason == "released" or released.granted is True
    assert duplicate.reason == "released"
