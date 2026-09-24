"""Canonical terminal WebSocket corpus and real-emitter parity tests."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.servers.websocket import broadcast as broadcast_module
from gobby.servers.websocket.proxy_relay import _map_host_frame
from gobby.servers.websocket.server import WebSocketServer
from gobby.servers.websocket.terminal_sizing import TerminalSizingMixin
from gobby.servers.websocket.terminal_ws import TerminalWsMixin
from gobby.servers.websocket.terminal_ws_control import TerminalControlMixin
from gobby.servers.websocket.terminal_ws_create import TerminalCreateMixin
from gobby.storage.machines import Machine
from gobby.storage.terminals import AttachLocator
from gobby.storage.workspaces import Workspace, WorkspacePane, WorkspaceTab
from gobby.terminals import web_spawn
from gobby.terminals.actor_scope import OPERATOR_ACTOR
from gobby.terminals.leases import HolderChange, TerminalLeaseRegistry
from gobby.terminals.runtime import (
    Delivered,
    IndeterminateWrite,
    PreparedSpawn,
    Suppressed,
    TerminalHandle,
    TerminalSpawnRequest,
)
from gobby.terminals.workspace_contract import WorkspaceEvent, WorkspaceOpError, WorkspaceSnapshot
from gobby.terminals.workspace_ops import WorkspaceOps
from gobby.terminals.ws_protocol import (
    TERMINAL_WS_SAFE_INTEGER_MAX,
    decode_message,
    encode_message,
    fragment_event,
)
from tests.servers.test_tmux_mixin import MockWebSocket

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "terminal_ws_golden"
OLD_GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "terminal_ws_golden"
DAEMON_EPOCH = "00000000-0000-4000-8000-000000000000"
TERMINAL_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ATTACHMENT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
HOLDER_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
WORKSPACE_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
TAB_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
PANE_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"
SPLIT_PANE_ID = "11111111-1111-4111-8111-111111111111"
SPLIT_TERMINAL_ID = "22222222-2222-4222-8222-222222222222"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"
MACHINE_ID = "55555555-5555-4555-8555-555555555555"
FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=UTC)

pytestmark = pytest.mark.unit


@dataclass
class _GoldenRow:
    id: str = TERMINAL_ID
    backend: str = "tmux"
    ownership: str = "gobby"
    state: str = "live"
    title: str | None = "sess"
    session_id: str | None = None
    agent_run_id: str | None = None
    rows: int | None = 24
    cols: int | None = 80
    locator_key: str | None = None
    created_at: datetime = FIXTURE_TIME
    updated_at: datetime = FIXTURE_TIME


class _GoldenManager:
    def __init__(self, row: _GoldenRow | None = None) -> None:
        self.row = row or _GoldenRow()

    def get(self, terminal_id: str) -> _GoldenRow | None:
        return self.row if terminal_id == self.row.id else None

    @asynccontextmanager
    async def settle_lock(self, _terminal_id: str) -> AsyncIterator[None]:
        yield

    def list_page(
        self,
        _project_ids: object,
        *,
        limit: int,
        **_kwargs: object,
    ) -> tuple[list[_GoldenRow], bool]:
        return [self.row][:limit], False

    def create_pending(
        self,
        terminal_id: str,
        _project_id: str,
        backend: str,
        _ownership: str,
        _spawn_key: str,
        **_kwargs: object,
    ) -> _GoldenRow:
        self.row.id = terminal_id
        self.row.backend = backend
        self.row.state = "pending"
        return self.row

    def promote_to_live(self, terminal_id: str, **_kwargs: object) -> _GoldenRow | None:
        row = self.get(terminal_id)
        if row is not None:
            row.state = "live"
        return row

    def fail_pending(self, terminal_id: str) -> _GoldenRow | None:
        row = self.get(terminal_id)
        if row is not None:
            row.state = "exited"
        return row

    def mark_exited(self, terminal_id: str) -> _GoldenRow | None:
        row = self.get(terminal_id)
        if row is None or row.state not in {"live", "orphaned"}:
            return None
        row.state = "exited"
        return row

    def set_dims(self, terminal_id: str, rows: int, cols: int) -> _GoldenRow | None:
        row = self.get(terminal_id)
        if row is not None:
            row.rows = rows
            row.cols = cols
        return row


class _GoldenRuntime:
    def __init__(
        self,
        backend: str = "tmux",
        *,
        refuse_spawn: bool = False,
        write_result: object | None = None,
    ) -> None:
        self.backend = backend
        self.refuse_spawn = refuse_spawn
        self.write_result = Delivered() if write_result is None else write_result
        self.locator = AttachLocator(
            backend=cast(Any, backend),
            frame_host_epoch="host-epoch-1",
            host_socket="/tmp/gobby-terminal.sock",
            host_terminal_id=TERMINAL_ID,
            socket_path="/tmp/tmux.sock" if backend == "tmux" else None,
            pane_id="%1" if backend == "tmux" else None,
            server_pid=100 if backend == "tmux" else None,
            server_start_time=200 if backend == "tmux" else None,
        )

    async def attach_locator(self, _row: object) -> AttachLocator:
        return self.locator

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
        if self.refuse_spawn:
            raise RuntimeError("backend refused")
        return PreparedSpawn(
            terminal_id=request.terminal_id,
            spawn_key=request.spawn_key,
            locator=self.locator,
            process=None,
            host_terminal_id=None,
            stored_locator={},
            locator_key="",
        )

    async def commit_spawn(self, prepared: PreparedSpawn) -> TerminalHandle:
        assert prepared.locator is not None
        return TerminalHandle(prepared.terminal_id, prepared.locator)

    async def terminate(self, _row: object, _grace_seconds: float) -> None:
        return None

    async def resize(self, _row: object, _rows: int, _cols: int) -> None:
        return None

    async def write_input(self, _row: object, _data: bytes) -> object:
        return self.write_result

    async def write_paste(self, _row: object, _text: str) -> Delivered:
        return Delivered()

    async def write_text(self, _row: object, _text: str, _submit: bool) -> Delivered:
        return Delivered()


def _server(
    *,
    backend: str = "tmux",
    refuse_spawn: bool = False,
    write_result: object | None = None,
) -> tuple[WebSocketServer, _GoldenManager, _GoldenRuntime]:
    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    server = WebSocketServer(config, MagicMock(), AsyncMock(return_value="test-user"))
    server.lease_registry = TerminalLeaseRegistry(daemon_epoch=DAEMON_EPOCH)
    manager = _GoldenManager(_GoldenRow(backend=backend))
    runtime = _GoldenRuntime(
        backend,
        refuse_spawn=refuse_spawn,
        write_result=write_result,
    )
    server.terminal_manager = manager
    server.terminal_runtime_registry = SimpleNamespace(resolve=lambda _backend: runtime)
    server.write_coordinator = SimpleNamespace(write=AsyncMock(return_value=runtime.write_result))
    server.terminal_config = SimpleNamespace(default_backend=backend)
    cast(Any, server)._sweep_tmux_panes = AsyncMock(return_value={})
    return server, manager, runtime


def _load(name: str) -> bytes:
    return (GOLDEN_DIR / name).read_bytes()


def _message(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_load(name)))


def _sent(websocket: MockWebSocket, index: int = -1) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(websocket.sent_messages[index]))


def _assert_golden(name: str, payload: dict[str, Any]) -> None:
    assert encode_message(payload) == _load(name), name


def _manifest_names() -> list[str]:
    manifest = cast(dict[str, object], json.loads(_load("manifest.json")))
    assert set(manifest) == {"fixtures"}
    fixtures = manifest["fixtures"]
    assert isinstance(fixtures, list)
    assert all(isinstance(name, str) for name in fixtures)
    return cast(list[str], fixtures)


async def _assert_write_outcome(
    name: str,
    *,
    write_result: object | None = None,
    primed_writes: dict[int, bytes] | None = None,
) -> None:
    server, _, _ = _server(write_result=write_result)
    registry = server.lease_registry
    await registry.attach(TERMINAL_ID, attachment_id=ATTACHMENT_ID)
    await registry.take_control(TERMINAL_ID, ATTACHMENT_ID)
    generation = registry.generation(TERMINAL_ID)
    for seq, payload in (primed_writes or {}).items():
        admitted = registry.admit_write(
            TERMINAL_ID,
            attachment_id=ATTACHMENT_ID,
            expected_lease_generation=generation,
            seq=seq,
            kind="input",
            payload=payload,
        )
        assert admitted.ok

    expected = _message(name)
    seq = expected["client_write_seq"]
    assert isinstance(seq, int)
    request = {**_message("input.json"), "client_write_seq": seq}
    websocket = MockWebSocket()
    await TerminalWsMixin._handle_terminal_input(server, websocket, request)
    _assert_golden(name, _sent(websocket))


def test_python_matches_terminal_ws_golden_corpus() -> None:
    names = _manifest_names()
    assert len(names) == len(set(names)) == 43
    assert "manifest.json" not in names
    on_disk = {path.name for path in GOLDEN_DIR.iterdir() if path.name != "manifest.json"}
    assert on_disk == set(names)
    assert len(list(GOLDEN_DIR.iterdir())) == 44
    assert not OLD_GOLDEN_DIR.exists() or not any(OLD_GOLDEN_DIR.iterdir())

    for name in names:
        raw = _load(name)
        message = decode_message(raw)
        assert encode_message(message) == raw, name
        assert "mode" not in message


@pytest.mark.asyncio
async def test_emitters_match_golden_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = MagicMock()
    clock.now.return_value = FIXTURE_TIME
    monkeypatch.setattr(broadcast_module, "datetime", clock)
    monkeypatch.setattr("gobby.terminals.leases.secrets.token_hex", lambda _size: ATTACHMENT_ID)
    monkeypatch.setattr(web_spawn, "mint_terminal_id", lambda: TERMINAL_ID)
    monkeypatch.setattr(
        "gobby.servers.websocket.terminal_ws.require_machine_id", lambda: "machine-1"
    )

    server, _, _ = _server()
    websocket = MockWebSocket()
    cast(Any, server)._start_proxy_attach = AsyncMock(return_value=None)
    await TerminalWsMixin._handle_terminal_attach(server, websocket, _message("attach.json"))
    _assert_golden("attach_result.json", _sent(websocket))

    server, _, _ = _server()
    websocket = MockWebSocket()
    cast(Any, server)._start_proxy_attach = AsyncMock(return_value=None)
    await TerminalWsMixin._handle_terminal_attach(
        server, websocket, _message("attach_semantic.json")
    )
    _assert_golden("attach_result.json", _sent(websocket))

    server, _, _ = _server()
    websocket = MockWebSocket()
    cast(Any, server)._start_proxy_attach = AsyncMock(return_value="runtime_unavailable")
    await TerminalWsMixin._handle_terminal_attach(server, websocket, _message("attach.json"))
    _assert_golden("attach_result_error.json", _sent(websocket))

    server, _, _ = _server(backend="native")
    websocket = MockWebSocket()
    direct_request = {
        **_message("attach.json"),
        "request_id": "req-attach-direct",
        "frame_delivery": "direct",
    }
    await TerminalWsMixin._handle_terminal_attach(server, websocket, direct_request)
    _assert_golden("attach_result_direct.json", _sent(websocket))

    server, _, _ = _server()
    websocket = MockWebSocket()
    await TerminalWsMixin._handle_terminal_list(server, websocket, _message("list.json"))
    _assert_golden("list_snapshot.json", _sent(websocket))

    server, _, _ = _server()
    websocket = MockWebSocket()
    await TerminalCreateMixin._handle_terminal_create(server, websocket, _message("create.json"))
    _assert_golden("create_result.json", _sent(websocket, 0))
    await server.lease_registry.shutdown_lifecycle_publication()

    server, _, _ = _server(refuse_spawn=True)
    websocket = MockWebSocket()
    await TerminalCreateMixin._handle_terminal_create(server, websocket, _message("create.json"))
    _assert_golden("create_result_refused.json", _sent(websocket))

    server, _, _ = _server()
    websocket = MockWebSocket()
    await TerminalCreateMixin._handle_terminal_kill(server, websocket, _message("kill.json"))
    _assert_golden("kill_result.json", _sent(websocket))
    await server.lease_registry.shutdown_lifecycle_publication()

    server, _, _ = _server()
    await server.lease_registry.attach(TERMINAL_ID, attachment_id=ATTACHMENT_ID)
    await server.lease_registry.take_control(TERMINAL_ID, ATTACHMENT_ID)
    websocket = MockWebSocket()
    await TerminalWsMixin._handle_terminal_detach(server, websocket, _message("detach.json"))
    _assert_golden("attachment_finalized.json", _sent(websocket, 0))
    _assert_golden("detach_result.json", _sent(websocket, 1))
    await server.lease_registry.shutdown_lifecycle_publication()

    server, _, _ = _server()
    await server.lease_registry.attach(TERMINAL_ID, attachment_id=ATTACHMENT_ID)
    websocket = MockWebSocket()
    await TerminalWsMixin._handle_terminal_set_scroll_offset(
        server, websocket, _message("set_scroll_offset.json")
    )
    _assert_golden("scroll_offset_applied.json", _sent(websocket))

    server, _, _ = _server()
    await server.lease_registry.attach(TERMINAL_ID, attachment_id=ATTACHMENT_ID)
    websocket = MockWebSocket()
    await TerminalControlMixin._handle_terminal_take_control(
        server, websocket, _message("take_control.json")
    )
    _assert_golden("control_result.json", _sent(websocket))

    await _assert_write_outcome("write_outcome.json")
    await _assert_write_outcome(
        "write_outcome_indeterminate.json",
        write_result=IndeterminateWrite(),
    )
    await _assert_write_outcome(
        "write_outcome_refused.json",
        write_result=Suppressed(action_key="golden"),
    )
    await _assert_write_outcome(
        "write_outcome_conflict.json",
        primed_writes={4: b"different"},
    )
    await _assert_write_outcome(
        "write_outcome_expired.json",
        primed_writes={6: b"future"},
    )
    await _assert_write_outcome(
        "write_outcome_capacity.json",
        primed_writes=dict.fromkeys(range(64), b"pending"),
    )

    server, manager, _ = _server()
    await server.lease_registry.attach(TERMINAL_ID, attachment_id=ATTACHMENT_ID)
    websocket = MockWebSocket()
    await TerminalSizingMixin._handle_terminal_resize(server, websocket, _message("resize.json"))
    assert websocket.sent_messages == []
    assert manager.get(TERMINAL_ID) == _GoldenRow()

    server, _, _ = _server()
    websocket = MockWebSocket()
    server.clients[websocket] = {}
    await server.broadcast_terminal_output(TERMINAL_ID, "ready.\n", ATTACHMENT_ID)
    _assert_golden("output.json", _sent(websocket))

    server, _, _ = _server()
    websocket = MockWebSocket()
    server.clients[websocket] = {}
    await server.broadcast_tmux_session_event("created", terminal_id=TERMINAL_ID)
    _assert_golden("event.json", _sent(websocket))
    await server.lease_registry.shutdown_lifecycle_publication()

    server, _, _ = _server()
    websocket = MockWebSocket()
    server.clients[websocket] = {}
    await server._fanout_lease_lost(ATTACHMENT_ID, HOLDER_ID, 2)
    _assert_golden("lease_lost.json", _sent(websocket))
    await server.lease_registry.shutdown_lifecycle_publication()

    server, _, _ = _server()
    websocket = MockWebSocket()
    await server._proxy().emit_lifecycle(
        websocket,
        {
            "type": "terminal_attachment_finalized",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "reason": "detach",
            "lease_generation": 2,
        },
    )
    _assert_golden("attachment_finalized.json", _sent(websocket))
    await server._proxy().drop_socket(websocket, "test_done")
    await server.lease_registry.shutdown_lifecycle_publication()

    history = _map_host_frame(
        {
            "type": "attach_history",
            "text": "ready.\n",
            "truncated": False,
            "dropped_bytes": 0,
            "total_bytes": 7,
        },
        TERMINAL_ID,
        ATTACHMENT_ID,
        "terminal_ansi",
    )
    assert history is not None
    _assert_golden("attach_history.json", history)

    terminal_frame = _map_host_frame(
        {"type": "frame", "raw": b"frame"},
        TERMINAL_ID,
        ATTACHMENT_ID,
        "semantic_frame",
    )
    assert terminal_frame is not None
    assert base64.b64decode(cast(str, terminal_frame["payload"])) == b"frame"
    _assert_golden("terminal_frame.json", terminal_frame)

    fragments = fragment_event(
        event="terminal_attach_history",
        terminal_id=TERMINAL_ID,
        attachment_id=ATTACHMENT_ID,
        message_seq=1,
        complete_json=encode_message(history),
    )
    _assert_golden("fragment.json", fragments[0])
    _assert_golden("fragment_last.json", fragments[1])


@pytest.mark.asyncio
async def test_workspace_emitters_match_golden_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = MagicMock()
    clock.now.return_value = FIXTURE_TIME
    monkeypatch.setattr(broadcast_module, "datetime", clock)
    tab = WorkspaceTab(
        id=TAB_ID,
        workspace_id=WORKSPACE_ID,
        ref=1,
        title=None,
        project_id=PROJECT_ID,
        worktree_id=None,
        position=0,
        focused_pane_id=SPLIT_PANE_ID,
        layout={
            "kind": "split",
            "axis": "horizontal",
            "ratio": 0.5,
            "children": [
                {"kind": "pane", "pane_id": PANE_ID},
                {"kind": "pane", "pane_id": SPLIT_PANE_ID},
            ],
        },
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
    )
    first, split = (
        WorkspacePane(
            id=pane_id,
            tab_id=TAB_ID,
            ref=ref,
            terminal_id=terminal_id,
            owns_terminal=True,
            label=None,
            created_at=FIXTURE_TIME,
            updated_at=FIXTURE_TIME,
        )
        for ref, pane_id, terminal_id in (
            (1, PANE_ID, TERMINAL_ID),
            (2, SPLIT_PANE_ID, SPLIT_TERMINAL_ID),
        )
    )
    home = Workspace(
        id=WORKSPACE_ID,
        machine_id=MACHINE_ID,
        ref=1,
        name="default",
        focused_project_id=PROJECT_ID,
        focused_tab_id=TAB_ID,
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
    )
    node = Machine(
        id=MACHINE_ID,
        hostname=None,
        os=None,
        label=None,
        tailscale_name=None,
        owner_user_id="test-user",
        first_seen=FIXTURE_TIME,
        last_seen=FIXTURE_TIME,
        ref=1,
    )
    reason = f"Pane {PANE_ID} is still spawning; retry after its split replies"
    ops = MagicMock(spec=WorkspaceOps)
    ops.workspace_snapshot = AsyncMock(
        return_value=WorkspaceSnapshot(node=node, workspace=home, tabs=(tab,), panes=(first, split))
    )
    ops.pane_split = AsyncMock(side_effect=WorkspaceOpError("busy", reason))
    server, _, _ = _server()
    server.workspace_ops = ops
    websocket = MockWebSocket()
    server.clients[websocket] = {}

    await server._handle_message(websocket, _load("workspace_attach.json").decode())
    _assert_golden("workspace_snapshot.json", _sent(websocket))
    ops.workspace_snapshot.assert_awaited_once_with(OPERATOR_ACTOR)

    await server._handle_message(websocket, _load("workspace_op.json").decode())
    _assert_golden("workspace_error.json", _sent(websocket))
    ops.pane_split.assert_awaited_once_with(OPERATOR_ACTOR, pane=PANE_ID, axis="horizontal")

    await server.broadcast_workspace_event(
        WorkspaceEvent(
            kind="pane.added",
            workspace_id=WORKSPACE_ID,
            workspace=None,
            tabs=[tab.to_dict()],
            panes=[split.to_dict()],
        )
    )
    _assert_golden("workspace_event.json", _sent(websocket))
    await server.lease_registry.shutdown_lifecycle_publication()


def test_seq_and_lease_generation_are_safe_integers() -> None:
    overflow = TERMINAL_WS_SAFE_INTEGER_MAX + 1
    with pytest.raises(ValueError, match="safe_integer_overflow"):
        encode_message({"type": "terminal_event", "seq": overflow})
    for name in ("fragment.json", "control_result.json", "input.json", "event.json"):
        message = _message(name)
        counters = [
            value
            for field, value in message.items()
            if field in {"message_seq", "lease_generation", "client_write_seq", "seq"}
        ]
        assert counters and all(isinstance(value, int) for value in counters)


def test_attachment_finalized_is_pinned() -> None:
    payload = _message("attachment_finalized.json")
    assert payload["type"] == "terminal_attachment_finalized"
    assert payload["reason"] in {
        "detach",
        "ws_close",
        "ws_loss",
        "proxy_frame_eof",
        "proxy_lag",
        "relay_overflow",
        "host_loss",
        "message_seq_overflow",
    }
    assert isinstance(payload["lease_generation"], int)


def test_control_result_pins_host_input_granted() -> None:
    message = _message("control_result.json")
    assert "host_input_granted" in message
    assert message["host_input_granted"] is None


@pytest.mark.asyncio
async def test_control_result_carries_the_holder_observer_answer() -> None:
    server, _, _ = _server()

    async def granting(_change: HolderChange) -> bool | None:
        return True

    server.lease_registry.set_holder_observer(granting)
    await server.lease_registry.attach(TERMINAL_ID, attachment_id=ATTACHMENT_ID)
    websocket = MockWebSocket()
    await TerminalControlMixin._handle_terminal_take_control(
        server, websocket, _message("take_control.json")
    )
    sent = _sent(websocket)
    assert sent["host_input_granted"] is True
    assert sent | {"host_input_granted": None} == _message("control_result.json")
