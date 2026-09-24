"""Workspace messages on the terminal WebSocket (plan gclient-workspaces 2.2)."""

from __future__ import annotations

import base64
import inspect
import itertools
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.websocket.server import WebSocketServer
from gobby.servers.websocket.workspace_ws import _fields
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager
from gobby.storage.workspaces import WorkspaceManager, WorkspaceNotFoundError
from gobby.terminals.leases import LifecyclePublicationError, TerminalLeaseRegistry
from gobby.terminals.runtime import PreparedSpawn, TerminalSpawnRequest
from gobby.terminals.workspace_contract import WorkspaceEvent, WorkspaceOpError
from gobby.terminals.workspace_ops import WorkspaceOps
from gobby.terminals.write_coordinator import WriteCoordinator
from gobby.terminals.ws_protocol import (
    TERMINAL_LIST_MAX_ENCODED_BYTES,
    TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES,
)
from gobby.utils.datetime import to_json_safe
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID
from tests.servers.test_tmux_mixin import MockWebSocket
from tests.terminals.fakes import FakeRuntime, runtime_registry

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000001"
DAEMON_EPOCH = "workspace-ws-epoch"
OPS = {
    "workspace.create",
    "workspace.list",
    "workspace.rename",
    "workspace.close",
    "workspace.set_focus_hints",
    "tab.create",
    "tab.rename",
    "tab.move",
    "tab.close",
    "pane.split",
    "pane.swap",
    "pane.move",
    "pane.resize",
    "pane.rename",
    "pane.close",
    "pane.send_text",
    "pane.send_keys",
    "pane.read",
    "pane.wait_for_output",
}
_REQUEST_IDS = itertools.count(1)


@dataclass
class _NativeRuntime(FakeRuntime):
    """Native fake whose host epoch follows each spawn, so live locator keys stay unique."""

    backend: Literal["tmux", "native"] = "native"

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
        self.host_epoch = str(request.terminal_id)
        return await super().prepare_spawn(request)


@dataclass
class _Stack:
    server: WebSocketServer
    workspaces: WorkspaceManager
    terminals: TerminalManager
    native: _NativeRuntime
    project_id: str


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _bare_server() -> WebSocketServer:
    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    return WebSocketServer(config, MagicMock(), AsyncMock(return_value="test-user"))


@pytest.fixture
async def stack(temp_db: HubDatabase, sample_project: dict[str, Any]) -> AsyncIterator[_Stack]:
    LocalMachineManager(temp_db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID, hostname="local")
    server = _bare_server()
    server.session_manager = SessionManager(temp_db)
    terminals = TerminalManager(temp_db)
    native = _NativeRuntime()
    registry = runtime_registry(FakeRuntime(backend="tmux"), native)
    leases = TerminalLeaseRegistry(daemon_epoch=DAEMON_EPOCH)
    workspaces = WorkspaceManager(temp_db)
    server.configure_terminals(
        terminals,
        registry,
        lease_registry=leases,
        write_coordinator=WriteCoordinator(terminals, registry, lease_registry=leases),
        workspace_manager=workspaces,
    )
    yield _Stack(server, workspaces, terminals, native, str(sample_project["id"]))
    await leases.shutdown_lifecycle_publication()


def _client(stack: _Stack, subscriptions: set[str]) -> MockWebSocket:
    websocket = MockWebSocket()
    websocket.subscriptions = subscriptions
    stack.server.clients[websocket] = {}
    return websocket


async def _request(
    server: WebSocketServer, websocket: MockWebSocket, message: dict[str, Any]
) -> dict[str, Any]:
    request_id = f"req-{next(_REQUEST_IDS)}"
    before = len(websocket.sent_messages)
    await server._handle_message(websocket, json.dumps({**message, "request_id": request_id}))
    replies = [
        reply
        for reply in websocket.all_messages()[before:]
        if reply.get("request_id") == request_id
    ]
    assert len(replies) == 1, websocket.sent_messages[before:]
    return replies[0]


async def _op(stack: _Stack, websocket: MockWebSocket, op: str, /, **fields: object) -> Any:
    reply = await _request(stack.server, websocket, {"type": "workspace_op", "op": op, **fields})
    assert reply["type"] == "workspace_op", reply
    assert reply["op"] == op
    return reply["result"]


async def _error(server: WebSocketServer, websocket: MockWebSocket, message: dict[str, Any]) -> str:
    reply = await _request(server, websocket, message)
    assert reply["type"] == "workspace_error", reply
    assert set(reply) == {"type", "request_id", "code", "reason"}
    assert isinstance(reply["reason"], str) and reply["reason"]
    return cast(str, reply["code"])


def _op_request(op: str, /, **fields: object) -> dict[str, Any]:
    return {"type": "workspace_op", "op": op, **fields}


def _lifecycle(websocket: MockWebSocket) -> list[dict[str, Any]]:
    return [
        message
        for message in websocket.all_messages()
        if message["type"] in {"workspace_event", "terminal_event"}
    ]


async def test_attach_creates_default_on_the_local_node(stack: _Stack) -> None:
    websocket = _client(stack, {"terminal_event"})
    with pytest.raises(WorkspaceNotFoundError):
        stack.workspaces.resolve_reference("default")

    reply = await _request(stack.server, websocket, {"type": "workspace_attach"})

    home = stack.workspaces.resolve_reference("default").workspace
    node = stack.workspaces.resolve_node()
    assert home.machine_id == LOCAL_MACHINE_ID and node.id == LOCAL_MACHINE_ID
    # Creating ``default`` published workspace.created as lifecycle seq 1 before the
    # rows were read, so the watermark covers it and the new subscriber never sees it.
    assert reply == {
        "type": "workspace_snapshot",
        "request_id": reply["request_id"],
        "workspace": {**to_json_safe(home.to_dict()), "node_ref": node.ref},
        "tabs": [],
        "panes": [],
        "snapshot": {"daemon_epoch": DAEMON_EPOCH, "seq": 1},
    }
    assert websocket.subscriptions == {"terminal_event", f"workspace_event:workspace_id={home.id}"}
    assert _lifecycle(websocket) == []

    created = await _op(
        stack,
        websocket,
        "tab.create",
        workspace=f"{node.ref}:{home.ref}",
        project_id=stack.project_id,
    )
    again = await _request(
        stack.server,
        websocket,
        {"type": "workspace_attach", "node": str(node.ref), "workspace": "default"},
    )
    tab, pane = created["tabs"][0], created["panes"][0]
    assert again["workspace"]["id"] == home.id and again["workspace"]["node_ref"] == node.ref
    assert again["tabs"] == [tab]
    assert tab["layout"] == {"kind": "pane", "pane_id": pane["id"]}
    assert again["panes"] == [pane]
    assert pane["ref"] == 0 and stack.terminals.get(pane["terminal_id"]) is not None
    assert again["snapshot"] == {"daemon_epoch": DAEMON_EPOCH, "seq": 2}
    assert [event["kind"] for event in _lifecycle(websocket)] == ["tab.created"]

    # workspace_snapshot re-serves the rows by id without registering a subscription.
    observer = _client(stack, set())
    served = await _request(
        stack.server, observer, {"type": "workspace_snapshot", "workspace": home.id}
    )
    assert served == {**again, "request_id": served["request_id"]}
    assert observer.subscriptions == set()
    missing = {"type": "workspace_attach", "workspace": "missing"}
    assert await _error(stack.server, observer, missing) == "not_found"
    assert observer.subscriptions == set()


@pytest.mark.parametrize(
    "message",
    [
        pytest.param({"type": "workspace_attach"}, id="attach"),
        pytest.param(
            {"type": "workspace_op", "op": "workspace.create"},
            id="mutation",
        ),
    ],
)
async def test_workspace_requests_refuse_after_lifecycle_publication_stops(
    stack: _Stack,
    message: dict[str, Any],
) -> None:
    websocket = _client(stack, set())
    await stack.server.lease_registry.shutdown_lifecycle_publication()
    stack.server.shutdown_in_progress = lambda: True

    reply = await _request(stack.server, websocket, message)

    assert reply["type"] == "workspace_error"
    assert reply["code"] == "shutdown_in_progress"
    assert reply["reason"] == "Daemon is shutting down"


async def test_attach_translates_lifecycle_close_when_shutdown_starts_mid_request(
    stack: _Stack,
) -> None:
    websocket = _client(stack, set())
    shutdown_states = iter((False, True))
    stack.server.shutdown_in_progress = lambda: next(shutdown_states)
    publication_stopped = LifecyclePublicationError("lifecycle publication stopped")

    with patch.object(
        stack.server,
        "_read_workspace",
        AsyncMock(side_effect=publication_stopped),
    ):
        reply = await _request(stack.server, websocket, {"type": "workspace_attach"})

    assert reply["type"] == "workspace_error"
    assert reply["code"] == "shutdown_in_progress"
    assert reply["reason"] == "Daemon is shutting down"


async def test_attach_propagates_lifecycle_publication_fault_while_running(
    stack: _Stack,
) -> None:
    websocket = _client(stack, set())
    stack.server.shutdown_in_progress = lambda: False
    publication_fault = LifecyclePublicationError("publisher failed")

    with (
        patch.object(
            stack.server,
            "_read_workspace",
            AsyncMock(side_effect=publication_fault),
        ),
        pytest.raises(LifecyclePublicationError, match="publisher failed"),
    ):
        await stack.server._handle_message(
            websocket,
            json.dumps({"type": "workspace_attach", "request_id": "req-fault"}),
        )


async def test_ops_round_trip_and_errors_are_typed(stack: _Stack) -> None:
    public_ops = {
        name.replace("_", ".", 1)
        for name, _member in inspect.getmembers(WorkspaceOps, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    assert public_ops - {"workspace.snapshot"} == OPS
    websocket = _client(stack, set())
    sent: set[str] = set()

    async def op(op_name: str, /, **fields: object) -> Any:
        sent.add(op_name)
        return await _op(stack, websocket, op_name, **fields)

    workspace = await op("workspace.create", name="ops")
    assert workspace["name"] == "ops" and workspace["machine_id"] == LOCAL_MACHINE_ID
    home = workspace["id"]
    listed = await op("workspace.list")
    assert any(row["id"] == home for row in listed)
    created = await op("tab.create", workspace=home, project_id=stack.project_id, title="one")
    tab, first = created["tabs"][0], created["panes"][0]
    second = (await op("pane.split", pane=first["id"], axis="horizontal"))["panes"][0]
    assert (await op("pane.swap", pane=first["id"], other=second["id"]))["id"] == tab["id"]
    assert (await op("pane.resize", pane=second["id"], ratio=0.25))["layout"]["ratio"] == 0.25
    assert (await op("pane.rename", pane=first["id"], label="main"))["label"] == "main"
    assert (await op("tab.rename", tab=tab["id"], title="renamed"))["title"] == "renamed"
    other = await op("tab.create", workspace=home, project_id=stack.project_id)
    other_tab, other_pane = other["tabs"][0], other["panes"][0]
    moved = await op("tab.move", tab=other_tab["id"], position=0)
    assert [row["position"] for row in moved["tabs"] if row["id"] == other_tab["id"]] == [0]
    await op("pane.move", pane=second["id"], tab=other_tab["id"], beside=other_pane["id"])
    assert (await op("workspace.rename", workspace=home, name="renamed"))["name"] == "renamed"
    hinted, focused = await op(
        "workspace.set_focus_hints",
        workspace=home,
        project_id=stack.project_id,
        tab=other_tab["id"],
        pane=other_pane["id"],
    )
    assert hinted["focused_tab_id"] == other_tab["id"] and focused["id"] == other_tab["id"]
    written = await op("pane.send_text", pane=first["id"], text="echo ready", submit=True)
    assert set(written) == {"idempotency_key", "indeterminate", "detail"}
    assert written["indeterminate"] is False
    keyed = await op("pane.send_keys", pane=first["id"], keys="ls\n", idempotency_key="keys-1")
    assert keyed["idempotency_key"] == "keys-1"
    stack.native.snapshot_text = "ready\n"
    assert "ready" in (await op("pane.read", pane=first["id"], lines=5))["text"]
    waited = await op(
        "pane.wait_for_output",
        pane=first["id"],
        pattern="ready",
        timeout_seconds=1,
        poll_interval_seconds=0.01,
    )
    assert (waited["matched"], waited["reason"]) == (True, "matched")
    closed = await op("pane.close", pane=second["id"])
    assert [row["id"] for row in closed["removed_panes"]] == [second["id"]]
    tab_closed = await op("tab.close", tab=other_tab["id"])
    assert [row["id"] for row in tab_closed["removed_tabs"]] == [other_tab["id"]]
    assert (await op("workspace.close", workspace=home))["id"] == home
    assert sent == OPS

    # Every typed ops failure becomes a workspace_error carrying its code.
    live = await _op(stack, websocket, "workspace.create")
    fill = await _op(
        stack, websocket, "tab.create", workspace=live["id"], project_id=stack.project_id
    )
    pane = fill["panes"][0]
    server = stack.server
    codes = [
        await _error(
            server, websocket, _op_request("pane.rename", pane=str(uuid.uuid4()), label="x")
        ),
        await _error(server, websocket, _op_request("pane.rename", pane="0:0:0:bogus", label="x")),
        await _error(
            server, websocket, _op_request("pane.split", pane=pane["id"], axis="diagonal")
        ),
        await _error(
            server,
            websocket,
            _op_request(
                "tab.create",
                workspace=live["id"],
                project_id=stack.project_id,
                terminal_id=pane["terminal_id"],
            ),
        ),
    ]
    stack.native.fail_spawn = True
    spawn = _op_request("tab.create", workspace=live["id"], project_id=stack.project_id)
    codes.append(await _error(server, websocket, spawn))
    ops = cast(Any, server).workspace_ops
    refusal = WorkspaceOpError("forbidden", "outside the actor's project")
    with patch.object(ops, "pane_read", AsyncMock(side_effect=refusal)):
        codes.append(await _error(server, websocket, _op_request("pane.read", pane=pane["id"])))
    assert codes == [
        "not_found",
        "invalid_ref",
        "invalid_op",
        "busy",
        "terminal_failed",
        "forbidden",
    ]

    # The envelope is checked against the op's signature before anything runs, and the
    # actor is never a client field.
    malformed = [
        {"type": "workspace_op"},
        _op_request("pane.explode", pane=pane["id"]),
        _op_request("workspace.snapshot"),
        _op_request("pane.split", pane=pane["id"]),
        _op_request("pane.resize", pane=pane["id"], ratio="wide"),
        _op_request("pane.read", pane=pane["id"], lines=0),
        _op_request("tab.move", tab=pane["tab_id"], position=True),
        _op_request("pane.rename", pane=pane["id"], label=7),
        _op_request("pane.close", pane=pane["id"], actor="session:intruder"),
        _op_request("pane.close", pane=pane["id"], extra=1),
        {"type": "workspace_attach", "workspace": 1},
    ]
    for message in malformed:
        assert await _error(server, websocket, message) == "invalid_op", message
    assert stack.workspaces.resolve_reference(pane["id"]).pane is not None

    unbounded = WorkspaceOpError(cast(Any, "x" * 300), "unbounded")
    with patch.object(ops, "pane_read", AsyncMock(side_effect=unbounded)):
        code = await _error(server, websocket, _op_request("pane.read", pane=pane["id"]))
    assert code == "x" * 128

    unconfigured = _bare_server()
    read = _op_request("pane.read", pane=pane["id"])
    assert await _error(unconfigured, MockWebSocket(), read) == "terminal_failed"


async def test_workspace_events_share_lifecycle_order_and_filter(stack: _Stack) -> None:
    watcher = _client(stack, {"terminal_event"})
    elsewhere = _client(stack, {"terminal_event"})
    bystander = _client(stack, {"terminal_event"})
    home = await _op(stack, bystander, "workspace.create", name="home")
    await _request(stack.server, watcher, {"type": "workspace_attach", "workspace": "home"})
    await _request(stack.server, elsewhere, {"type": "workspace_attach"})

    created = await _op(
        stack, watcher, "tab.create", workspace=home["id"], project_id=stack.project_id
    )
    pane = created["panes"][0]
    kill = {"type": "terminal_kill", "terminal_id": pane["terminal_id"]}
    assert (await _request(stack.server, bystander, kill))["success"] is True
    served = await _request(
        stack.server, watcher, {"type": "workspace_snapshot", "workspace": "home"}
    )

    stream = _lifecycle(watcher)
    assert [(message["type"], message.get("kind", message.get("event"))) for message in stream] == [
        ("workspace_event", "tab.created"),
        ("terminal_event", "killed"),
        ("workspace_event", "pane.removed"),
        ("workspace_event", "tab.removed"),
    ]
    first = stream[0]["seq"]
    assert [message["seq"] for message in stream] == list(range(first, first + 4))
    assert {message["daemon_epoch"] for message in stream} == {DAEMON_EPOCH}
    assert stream[1]["terminal_id"] == pane["terminal_id"]
    assert [row["terminal_id"] for row in stream[2]["panes"]] == [pane["terminal_id"]]
    assert {stream[index]["workspace_id"] for index in (0, 2, 3)} == {home["id"]}
    assert (stream[0]["project_id"], stream[3]["project_id"]) == (stack.project_id,) * 2
    assert served["snapshot"] == {"daemon_epoch": DAEMON_EPOCH, "seq": first + 3}
    assert (served["tabs"], served["panes"]) == ([], [])

    for other in (elsewhere, bystander):
        assert [(message["type"], message["seq"]) for message in _lifecycle(other)] == [
            ("terminal_event", first + 1)
        ]


async def test_oversized_workspace_events_fragment_without_faulting_publication(
    stack: _Stack,
) -> None:
    # Storage truncates names, titles, and labels, so only an event carrying very many
    # rows grows this large; one oversized row stands in for them.
    watcher = _client(stack, set())
    bystander = _client(stack, {"terminal_event"})
    home = (await _request(stack.server, watcher, {"type": "workspace_attach"}))["workspace"]["id"]
    row = {"id": str(uuid.uuid4()), "label": "x" * TERMINAL_LIST_MAX_ENCODED_BYTES}
    before = len(watcher.sent_messages)

    await stack.server.broadcast_workspace_event(
        WorkspaceEvent(kind="pane.renamed", workspace_id=home, workspace=None, tabs=[], panes=[row])
    )

    fragments = [json.loads(raw) for raw in watcher.sent_messages[before:]]
    assert len(fragments) >= 2
    assert {message["type"] for message in fragments} == {"terminal_ws_fragment"}
    seq = fragments[0]["message_seq"]
    assert [message["fragment_index"] for message in fragments] == list(range(len(fragments)))
    assert [message["more"] for message in fragments] == [True] * (len(fragments) - 1) + [False]
    for message in fragments:
        assert message["event"] == "workspace_event"
        assert message["attachment_id"] == message["terminal_id"] == home
        assert message["message_seq"] == seq
    event = json.loads(b"".join(base64.b64decode(message["payload"]) for message in fragments))
    assert (event["type"], event["kind"], event["seq"]) == ("workspace_event", "pane.renamed", seq)
    assert (event["workspace_id"], event["panes"]) == (home, [row])
    assert all(
        message["type"] not in {"workspace_event", "terminal_ws_fragment"}
        for message in bystander.all_messages()
    )

    # Rows too large to reassemble go out without them, and publication keeps running.
    before = len(watcher.sent_messages)
    oversized = {**row, "label": "y" * TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES}
    await stack.server.broadcast_workspace_event(
        WorkspaceEvent(
            kind="pane.renamed", workspace_id=home, workspace=None, tabs=[], panes=[oversized]
        )
    )
    await stack.server.broadcast_workspace_event(
        WorkspaceEvent(
            kind="workspace.renamed", workspace_id=home, workspace=None, tabs=[], panes=[]
        )
    )
    rowless, after = (json.loads(raw) for raw in watcher.sent_messages[before:])
    assert (rowless["kind"], rowless["workspace"], rowless["tabs"], rowless["panes"]) == (
        "pane.renamed",
        None,
        [],
        [],
    )
    assert (rowless["seq"], after["seq"]) == (seq + 1, seq + 2)
    assert stack.server.lease_registry.lifecycle_snapshot()["seq"] == seq + 2


def test_op_fields_refuse_types_a_message_cannot_be_checked_against() -> None:
    async def send_many(self: object, actor: str, keys: list[str]) -> None: ...

    async def pick_axis(self: object, actor: str, axis: Literal["horizontal"]) -> None: ...

    for op in (send_many, pick_axis):
        with pytest.raises(TypeError, match=rf"WorkspaceOps\.{op.__name__}\.\w+: unsupported"):
            _fields(op)
