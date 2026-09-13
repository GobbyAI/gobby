"""Lifecycle watermark ordering and terminal-list snapshot acceptance tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.http import HTTPServer
from gobby.servers.routes import terminals as terminal_routes
from gobby.servers.routes.terminals import create_terminals_router
from gobby.servers.websocket import server as websocket_server_module
from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.terminals import ws_protocol
from gobby.terminals.leases import LifecyclePublicationError, TerminalLeaseRegistry
from gobby.terminals.web_spawn import WebSpawnResult
from tests.servers.test_tmux_mixin import MockWebSocket
from tests.storage.test_terminals import LOCAL_MACHINE_ID, _create_pending, _manager

pytestmark = pytest.mark.unit


def _server() -> WebSocketServer:
    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    server = WebSocketServer(config, MagicMock(), AsyncMock(return_value="test-user"))
    server.lease_registry = TerminalLeaseRegistry()
    return server


async def _wait_for(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


def _lifecycle_messages(websocket: MockWebSocket) -> list[dict[str, Any]]:
    return [message for message in websocket.all_messages() if "seq" in message]


class _GateWebSocket(MockWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.send_started = asyncio.Event()
        self.send_release = asyncio.Event()
        self._blocked = False

    async def send(self, message: str) -> None:
        if not self._blocked:
            self._blocked = True
            self.send_started.set()
            await self.send_release.wait()
        await super().send(message)


@dataclass
class _Row:
    id: str = "term-1"
    backend: str = "native"
    ownership: str = "gobby"
    state: str = "live"
    machine_id: str = LOCAL_MACHINE_ID
    project_id: str = "project-1"
    created_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    updated_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    locator_key: str | None = None
    title: str | None = "terminal"
    session_id: str | None = None
    agent_run_id: str | None = None
    rows: int | None = 24
    cols: int | None = 80
    host_epoch: str | None = None


class _LifecycleManager:
    def __init__(self, row: _Row) -> None:
        self.row = row
        self.exited = False

    def get(self, terminal_id: str) -> _Row | None:
        return self.row if terminal_id == self.row.id else None

    def mark_exited(self, terminal_id: str) -> _Row | None:
        if terminal_id != self.row.id or self.exited or self.row.state not in {"live", "orphaned"}:
            return None
        self.exited = True
        self.row.state = "exited"
        return self.row


class _PageManager:
    def __init__(self, rows: list[_Row]) -> None:
        self.rows = rows

    def list_page(
        self,
        *_args: Any,
        cursor_id: str | None = None,
        limit: int,
        **_kwargs: Any,
    ) -> tuple[list[_Row], bool]:
        start = 0
        if cursor_id is not None:
            start = next(index + 1 for index, row in enumerate(self.rows) if row.id == cursor_id)
        page = self.rows[start : start + limit]
        return page, start + len(page) < len(self.rows)

    def attach_locator(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Listener:
    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


async def test_snapshot_orders_lifecycle_events(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID)
    manager = _manager(temp_db)
    _create_pending(manager, sample_project["id"])
    _create_pending(manager, sample_project["id"])
    server = _server()
    server.terminal_manager = manager
    monkeypatch.setattr(server, "_sweep_tmux_panes", AsyncMock(return_value={}))

    before_socket = MockWebSocket()
    server.clients[before_socket] = {}
    await server.broadcast_tmux_session_event("created", terminal_id="before")
    before = before_socket.last_message()

    first_socket = MockWebSocket()
    await server._handle_terminal_list(first_socket, {"request_id": "first", "limit": 1})
    first = first_socket.last_message()
    assert first["snapshot"]["daemon_epoch"] == before["daemon_epoch"]
    assert before["seq"] <= first["snapshot"]["seq"]
    assert first["next_cursor"] is not None

    after_socket = MockWebSocket()
    server.clients = {after_socket: {}}
    await server.broadcast_tmux_session_event("killed", terminal_id="after")
    after = after_socket.last_message()
    assert after["seq"] > first["snapshot"]["seq"]

    continuation_socket = MockWebSocket()
    await server._handle_terminal_list(
        continuation_socket,
        {"request_id": "next", "limit": 1, "cursor": first["next_cursor"]},
    )
    assert continuation_socket.last_message()["snapshot"] is None

    http_server = SimpleNamespace(
        services=SimpleNamespace(terminal_manager=manager, websocket_server=server),
        websocket_server=server,
    )
    app = FastAPI()
    app.include_router(create_terminals_router(cast(HTTPServer, http_server)))
    with TestClient(app) as client:
        rest_first = client.get(
            "/api/terminals",
            params={"project_id": sample_project["id"], "limit": 1},
        ).json()
        assert rest_first["snapshot"] == server.lease_registry.lifecycle_snapshot()
        rest_next = client.get(
            "/api/terminals",
            params={
                "project_id": sample_project["id"],
                "limit": 1,
                "cursor": rest_first["next_cursor"],
            },
        ).json()
        assert rest_next["snapshot"] is None

    await server.lease_registry.shutdown_lifecycle_publication()


async def test_every_lifecycle_emitter_is_stamped() -> None:
    server = _server()
    websocket = MockWebSocket()
    server.clients[websocket] = {}

    await server.broadcast_tmux_session_event("created", terminal_id="term-1")
    await server._fanout_lease_lost("attachment-1", "attachment-2", 2)
    await server._proxy().emit_lifecycle(
        websocket,
        {
            "type": "terminal_attachment_finalized",
            "terminal_id": "term-1",
            "attachment_id": "attachment-1",
            "reason": "detach",
            "lease_generation": 2,
        },
    )
    await server._broadcast_tmux_event("session_created", "legacy", "default")

    messages = _lifecycle_messages(websocket)
    assert [message["seq"] for message in messages] == [1, 2, 3, 4]
    assert len({message["daemon_epoch"] for message in messages}) == 1
    assert {message["type"] for message in messages} == {
        "terminal_event",
        "terminal_lease_lost",
        "terminal_attachment_finalized",
    }

    previous_epoch = server.lease_registry.daemon_epoch
    server.lease_registry._lifecycle_seq = ws_protocol.TERMINAL_WS_SAFE_INTEGER_MAX
    await server.broadcast_tmux_session_event("created", terminal_id="rotated")
    rotated = _lifecycle_messages(websocket)[-1]
    assert rotated["seq"] == 1
    assert rotated["daemon_epoch"] != previous_epoch

    await server._proxy().drop_socket(websocket, "test_done")
    await server.lease_registry.shutdown_lifecycle_publication()


async def test_publication_order_matches_sequence_under_forced_yields() -> None:
    server = _server()
    websocket = _GateWebSocket()
    server.clients[websocket] = {}

    terminal_event = asyncio.create_task(
        server.broadcast_tmux_session_event("created", terminal_id="term-1")
    )
    await websocket.send_started.wait()
    lease_event = asyncio.create_task(server._fanout_lease_lost("a-1", "a-2", 2))
    finalized_event = asyncio.create_task(
        server._proxy().emit_lifecycle(
            websocket,
            {
                "type": "terminal_attachment_finalized",
                "terminal_id": "term-1",
                "attachment_id": "a-1",
                "reason": "detach",
                "lease_generation": 2,
            },
        )
    )
    await asyncio.sleep(0)

    list_socket = MockWebSocket()
    server.terminal_manager = None
    await server._handle_terminal_list(list_socket, {"request_id": "during"})
    assert list_socket.last_message()["snapshot"]["seq"] == 0

    websocket.send_release.set()
    await asyncio.gather(terminal_event, lease_event, finalized_event)
    messages = _lifecycle_messages(websocket)
    assert [message["seq"] for message in messages] == [1, 2, 3]
    reducer = {message["type"]: message["seq"] for message in messages}
    assert reducer == {
        "terminal_event": 1,
        "terminal_lease_lost": 2,
        "terminal_attachment_finalized": 3,
    }
    assert server.lease_registry.lifecycle_snapshot()["seq"] == 3

    await server._proxy().drop_socket(websocket, "test_done")
    await server.lease_registry.shutdown_lifecycle_publication()


async def test_direct_lifecycle_fallbacks_are_ordered() -> None:
    server = _server()
    detached = await server.lease_registry.attach("term-1", attachment_id="attachment-1")
    detach_socket = MockWebSocket()
    await server._handle_terminal_detach(
        detach_socket,
        {"terminal_id": "term-1", "attachment_id": detached.attachment_id},
    )

    previous = await server.lease_registry.attach("term-2", attachment_id="previous")
    replacement = await server.lease_registry.attach("term-2", attachment_id="replacement")
    assert (await server.lease_registry.take_control("term-2", previous.attachment_id)).granted
    requester = MockWebSocket()
    await server._handle_terminal_take_control(
        requester,
        {
            "terminal_id": "term-2",
            "attachment_id": replacement.attachment_id,
            "takeover": True,
        },
    )

    lifecycle = _lifecycle_messages(detach_socket) + _lifecycle_messages(requester)
    assert [message["seq"] for message in lifecycle] == [1, 2]
    assert [message["type"] for message in lifecycle] == [
        "terminal_attachment_finalized",
        "terminal_lease_lost",
    ]
    await server.lease_registry.shutdown_lifecycle_publication()


async def test_create_and_kill_publish_ordered_lifecycle_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server()
    row = _Row()
    manager = _LifecycleManager(row)
    runtime = SimpleNamespace(backend="native", terminate=AsyncMock())
    server.terminal_manager = manager
    server.terminal_runtime_registry = SimpleNamespace(resolve=lambda _backend: runtime)
    server.terminal_config = SimpleNamespace(default_backend="native")
    monkeypatch.setattr(
        "gobby.terminals.web_spawn.spawn_web_terminal",
        AsyncMock(return_value=WebSpawnResult(True, row.id)),
    )
    websocket = MockWebSocket()
    server.clients[websocket] = {}

    await server._handle_terminal_create(
        websocket,
        {"request_id": "create", "rows": 24, "cols": 80, "command": ["zsh"]},
    )
    messages = websocket.all_messages()
    assert [message["type"] for message in messages] == [
        "terminal_create_result",
        "terminal_event",
    ]
    assert messages[1]["event"] == "created"
    assert messages[1]["terminal"] == ws_protocol.inventory_item(row)
    assert messages[1]["seq"] == 1

    await server._handle_terminal_kill(websocket, {"request_id": "kill", "terminal_id": row.id})
    await server._handle_terminal_kill(
        websocket, {"request_id": "duplicate", "terminal_id": row.id}
    )
    terminal_events = websocket.messages_of_type("terminal_event")
    assert [(message["event"], message["seq"]) for message in terminal_events] == [
        ("created", 1),
        ("killed", 2),
    ]
    runtime.terminate.assert_awaited_once()

    refused = _server()
    refused.terminal_manager = None
    refused_socket = MockWebSocket()
    await refused._handle_terminal_create(
        refused_socket,
        {"request_id": "refused", "rows": 24, "cols": 80},
    )
    assert refused_socket.messages_of_type("terminal_event") == []

    await server.lease_registry.shutdown_lifecycle_publication()


async def test_proxy_relay_ack_precedes_committed_high_water() -> None:
    server = _server()
    websocket = _GateWebSocket()
    proxy_event = asyncio.create_task(
        server._proxy().emit_lifecycle(
            websocket,
            {
                "type": "terminal_attachment_finalized",
                "terminal_id": "term-1",
                "attachment_id": "a-1",
                "reason": "detach",
                "lease_generation": 1,
            },
        )
    )
    await websocket.send_started.wait()
    server.clients[websocket] = {}
    direct_event = asyncio.create_task(
        server.broadcast_tmux_session_event("created", terminal_id="term-2")
    )
    await asyncio.sleep(0)
    assert server.lease_registry.lifecycle_snapshot()["seq"] == 0
    assert websocket.sent_messages == []

    websocket.send_release.set()
    await asyncio.gather(proxy_event, direct_event)
    assert [message["seq"] for message in _lifecycle_messages(websocket)] == [1, 2]

    registry = TerminalLeaseRegistry()
    release = asyncio.Event()
    started = asyncio.Event()
    published: list[dict[str, Any]] = []

    async def blocked_publisher(event: dict[str, Any]) -> None:
        started.set()
        await release.wait()
        published.append(event)

    first = asyncio.create_task(registry.publish_lifecycle({"name": "first"}, blocked_publisher))
    await started.wait()
    queued = [
        asyncio.create_task(
            registry.publish_lifecycle(
                {"name": f"queued-{index}"},
                lambda event: _append_after_yield(published, event),
            )
        )
        for index in range(256)
    ]
    await _wait_for(lambda: registry.lifecycle_queue_size == 256)
    extra = asyncio.create_task(
        registry.publish_lifecycle(
            {"name": "extra"}, lambda event: _append_after_yield(published, event)
        )
    )
    await asyncio.sleep(0)
    assert not extra.done()
    queued[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued[0]

    release.set()
    await asyncio.gather(first, *queued[1:], extra)
    assert len(published) == 258
    assert [event["seq"] for event in published] == list(range(1, 259))
    assert registry.lifecycle_snapshot()["seq"] == 258

    await registry.shutdown_lifecycle_publication()
    await server._proxy().drop_socket(websocket, "test_done")
    await server.lease_registry.shutdown_lifecycle_publication()


async def _append_after_yield(target: list[dict[str, Any]], event: dict[str, Any]) -> None:
    await asyncio.sleep(0)
    target.append(event)


async def test_publication_worker_lifecycle_settles_all_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = TerminalLeaseRegistry()
    started = asyncio.Event()
    never = asyncio.Event()

    async def blocked(_event: dict[str, Any]) -> None:
        started.set()
        await never.wait()

    current = asyncio.create_task(registry.publish_lifecycle({"name": "current"}, blocked))
    await started.wait()
    queued = asyncio.create_task(registry.publish_lifecycle({"name": "queued"}, blocked))
    await _wait_for(lambda: registry.lifecycle_queue_size == 1)
    await registry.shutdown_lifecycle_publication()
    publication_error = LifecyclePublicationError
    for task in (current, queued):
        with pytest.raises(publication_error):
            await task
    assert registry.lifecycle_worker is None

    faulted = TerminalLeaseRegistry()

    async def fail(_event: dict[str, Any]) -> None:
        raise RuntimeError("publisher failed")

    failed = asyncio.create_task(faulted.publish_lifecycle({"name": "failed"}, fail))
    behind = asyncio.create_task(faulted.publish_lifecycle({"name": "behind"}, blocked))
    for task in (failed, behind):
        with pytest.raises(publication_error):
            await task
    with pytest.raises(publication_error):
        await faulted.publish_lifecycle({"name": "future"}, blocked)

    server = _server()
    listener = _Listener()
    monkeypatch.setattr(websocket_server_module, "serve", AsyncMock(return_value=listener))
    monkeypatch.setattr(server, "_cleanup_tmux", AsyncMock())
    monkeypatch.setattr(server, "cleanup_voice", AsyncMock())
    await server.start()
    assert server.lease_registry.lifecycle_worker is not None
    await server.stop()
    assert server.lease_registry.lifecycle_worker is None


async def test_preadmission_close_race_and_worker_restart() -> None:
    registry = TerminalLeaseRegistry()
    release = asyncio.Event()
    started = asyncio.Event()

    async def blocked(_event: dict[str, Any]) -> None:
        started.set()
        await release.wait()

    first = asyncio.create_task(registry.publish_lifecycle({"name": "first"}, blocked))
    await started.wait()
    queued = [
        asyncio.create_task(registry.publish_lifecycle({"name": str(index)}, blocked))
        for index in range(256)
    ]
    await _wait_for(lambda: registry.lifecycle_queue_size == 256)
    preadmission = asyncio.create_task(
        registry.publish_lifecycle({"name": "preadmission"}, blocked)
    )
    await asyncio.sleep(0)
    assert not preadmission.done()

    await registry.shutdown_lifecycle_publication()
    publication_error = LifecyclePublicationError
    results = await asyncio.gather(first, *queued, preadmission, return_exceptions=True)
    assert all(isinstance(result, publication_error) for result in results)
    assert registry.lifecycle_queue_size == 0

    epoch = registry.daemon_epoch
    registry.start_lifecycle_publication()
    published: list[dict[str, Any]] = []
    restarted = await registry.publish_lifecycle(
        {"name": "restarted"}, lambda event: _append_after_yield(published, event)
    )
    assert registry.daemon_epoch == epoch
    assert restarted["seq"] > 0
    assert registry.lifecycle_snapshot()["seq"] == restarted["seq"]
    await registry.shutdown_lifecycle_publication()


async def test_byte_cap_truncation_preserves_forward_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = {"daemon_epoch": str(uuid.uuid4()), "seq": 3}
    envelope = {"type": "terminal_list", "request_id": "page"}
    terminal_ids = [str(uuid.UUID(int=index + 1)) for index in range(3)]
    cursors = [
        f"2026-01-0{index + 1}T00:00:00+00:00|{terminal_id}"
        for index, terminal_id in enumerate(terminal_ids)
    ]
    rows = [
        {"terminal_id": terminal_ids[0], "title": "one"},
        {"terminal_id": terminal_ids[1], "title": "two"},
        {"terminal_id": terminal_ids[2], "title": "x" * 500},
    ]

    exact_payload = {
        **envelope,
        "items": [rows[0]],
        "next_cursor": None,
        "snapshot": snapshot,
    }
    exact_cap = len(json.dumps(exact_payload, separators=(",", ":")).encode())
    monkeypatch.setattr(ws_protocol, "TERMINAL_LIST_MAX_ENCODED_BYTES", exact_cap)
    exact = ws_protocol.encode_page(
        rows[:1], None, snapshot=snapshot, item_cursors=cursors[:1], envelope=envelope
    )
    assert exact == exact_payload

    truncated_payload = {
        **envelope,
        "items": rows[:2],
        "next_cursor": cursors[1],
        "snapshot": snapshot,
    }
    truncated_cap = len(json.dumps(truncated_payload, separators=(",", ":")).encode())
    monkeypatch.setattr(ws_protocol, "TERMINAL_LIST_MAX_ENCODED_BYTES", truncated_cap)
    truncated = ws_protocol.encode_page(
        rows, None, snapshot=snapshot, item_cursors=cursors, envelope=envelope
    )
    assert truncated == truncated_payload
    assert ws_protocol.parse_list_cursor(truncated["next_cursor"])[1] == terminal_ids[1]

    monkeypatch.setattr(ws_protocol, "TERMINAL_LIST_MAX_ENCODED_BYTES", 10_000)
    resumed = ws_protocol.encode_page(
        rows[2:], None, snapshot=None, item_cursors=cursors[2:], envelope=envelope
    )
    assert resumed["items"][0]["terminal_id"] == terminal_ids[2]

    monkeypatch.setattr(ws_protocol, "TERMINAL_LIST_MAX_ENCODED_BYTES", 100)
    with pytest.raises(ws_protocol.TerminalPageTooLargeError):
        ws_protocol.encode_page(
            rows[2:], None, snapshot=snapshot, item_cursors=cursors[2:], envelope=envelope
        )

    page_row = _Row(title="x" * 500)
    server = _server()
    server.terminal_manager = _PageManager([page_row])
    monkeypatch.setattr(server, "_sweep_tmux_panes", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "gobby.servers.websocket.terminal_ws.require_machine_id", lambda: LOCAL_MACHINE_ID
    )
    websocket = MockWebSocket()
    await server._handle_terminal_list(websocket, {"request_id": "too-large"})
    assert websocket.last_message() == {
        "type": "terminal_error",
        "code": "terminal_page_too_large",
        "request_id": "too-large",
    }

    route_server = SimpleNamespace()
    route_server.websocket_server = server
    route_server.services = SimpleNamespace(
        terminal_manager=_PageManager([page_row]), websocket_server=server
    )
    monkeypatch.setattr(terminal_routes, "TerminalManager", _PageManager)
    app = FastAPI()
    app.include_router(create_terminals_router(cast(HTTPServer, route_server)))
    with TestClient(app) as client:
        response = client.get("/api/terminals", params={"project_id": "project-1"})
    assert response.status_code == 413
    assert response.json()["detail"] == "terminal_page_too_large"

    await server.lease_registry.shutdown_lifecycle_publication()
