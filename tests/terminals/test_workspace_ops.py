"""WorkspaceOps contract (plan gclient-workspaces 2.1)."""

from __future__ import annotations

import asyncio
import itertools
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import MagicMock, patch

import pytest
from psycopg import OperationalError

from gobby.agents.constants import (
    GOBBY_NODE_ID,
    GOBBY_NODE_REF,
    GOBBY_PANE_ID,
    GOBBY_PANE_REF,
    GOBBY_TAB_ID,
    GOBBY_WORKSPACE_ID,
)
from gobby.agents.detection.registry import (
    DetectionManifestRegistry,
    sync_bundled_detection_manifests,
)
from gobby.agents.idle_detector import IdleDetector
from gobby.mcp_proxy.tools.sessions._terminal_send_keys import _authorize_send_keys_target
from gobby.servers.websocket.workspace_ws import _OP_ENVELOPE, _arguments, _result
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.project_checkouts import require_root
from gobby.storage.projects import PERSONAL_PROJECT_ID, LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import (
    Terminal,
    TerminalManager,
    native_locator_key,
    tmux_locator_key,
)
from gobby.storage.workspaces import (
    LayoutChange,
    WorkspaceManager,
    WorkspaceNotFoundError,
    WorkspacePane,
    layout_pane_ids,
)
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.terminals.actor_scope import ActorScope
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.pane_io import _verified_submits
from gobby.terminals.runtime import (
    Delivered,
    IndeterminateWrite,
    PreparedSpawn,
    TerminalRuntimeRegistry,
    TerminalSpawnRequest,
)
from gobby.terminals.workspace_contract import WorkspaceEvent, WorkspaceOpError
from gobby.terminals.workspace_ops import WorkspaceOps
from gobby.terminals.write_coordinator import WriteCoordinator
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID
from tests.terminals.fakes import FakeRuntime, runtime_registry

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000001"
REMOTE_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000002"
OPERATOR = "operator"
ERROR_CODES = {"not_found", "invalid_ref", "invalid_op", "terminal_failed", "busy", "forbidden"}
_TMUX_PANE_NUMBERS = itertools.count(1)


@dataclass
class _HostRuntime(FakeRuntime):
    """Native fake whose host epoch follows each spawn, so live locator keys stay unique."""

    backend: Literal["tmux", "native"] = "native"
    spawning: asyncio.Event = field(default_factory=asyncio.Event)

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
        self.host_epoch = str(request.terminal_id)
        self.spawning.set()
        return await super().prepare_spawn(request)


@dataclass
class _Harness:
    db: HubDatabase
    workspaces: WorkspaceManager
    terminals: TerminalManager
    sessions: SessionManager
    native: _HostRuntime
    tmux: FakeRuntime
    events: list[WorkspaceEvent]
    project_id: str
    checkout: str
    ops: WorkspaceOps = field(init=False)

    def build_ops(self, registry: TerminalRuntimeRegistry) -> WorkspaceOps:
        async def record(event: WorkspaceEvent) -> None:
            self.events.append(event)

        return WorkspaceOps(
            workspaces=self.workspaces,
            terminals=self.terminals,
            registry=registry,
            coordinator=WriteCoordinator(
                self.terminals,
                registry,
                lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
            ),
            sessions=self.sessions,
            publish=record,
        )


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def harness(temp_db: HubDatabase, sample_project: dict[str, Any]) -> _Harness:
    LocalMachineManager(temp_db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID, hostname="local")
    project_id = str(sample_project["id"])
    native, tmux = _HostRuntime(), FakeRuntime(backend="tmux")
    built = _Harness(
        db=temp_db,
        workspaces=WorkspaceManager(temp_db),
        terminals=TerminalManager(temp_db),
        sessions=SessionManager(temp_db),
        native=native,
        tmux=tmux,
        events=[],
        project_id=project_id,
        checkout=require_root(temp_db, project_id, LOCAL_MACHINE_ID),
    )
    built.ops = built.build_ops(runtime_registry(tmux, native))
    return built


def _live_terminal(
    terminals: TerminalManager,
    project_id: str,
    backend: Literal["tmux", "native"],
    *,
    session_id: str | None = None,
) -> Terminal:
    terminal_id = str(uuid.uuid4())
    pending = terminals.create_pending(
        terminal_id,
        project_id,
        backend,
        "gobby",
        terminal_id,
        machine_id=LOCAL_MACHINE_ID,
        session_id=session_id,
    )
    if backend == "native":
        host_terminal_id = str(uuid.uuid4())
        live = terminals.promote_to_live(
            pending.id,
            locator={"host_terminal_id": host_terminal_id},
            locator_key=native_locator_key("adopted", host_terminal_id),
            host_epoch="adopted",
        )
    else:
        pane_id = f"%{next(_TMUX_PANE_NUMBERS)}"
        locator: dict[str, object] = {
            "socket_path": "/private/tmp/tmux-501/workspace-ops",
            "server_pid": 1658,
            "server_start_time": 1784592177,
            "pane_id": pane_id,
        }
        live = terminals.promote_to_live(
            pending.id,
            locator=locator,
            locator_key=tmux_locator_key(
                socket_path="/private/tmp/tmux-501/workspace-ops",
                server_pid=1658,
                server_start_time=1784592177,
                pane_id=pane_id,
            ),
            session_name=pending.spawn_key,
        )
    assert live is not None
    return live


def _session(h: _Harness, name: str, project_id: str, parent: Session | None = None) -> Session:
    return h.sessions.register(
        external_id=f"workspace-ops-{name}",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project_id,
        parent_session_id=None if parent is None else parent.id,
    )


def _autonomous_session(h: _Harness, parent: Session) -> Session:
    child = _session(h, "autonomous", h.project_id, parent)
    run = LocalAgentRunManager(h.db).create(
        parent_session_id=parent.id,
        provider="claude",
        prompt="work",
        child_session_id=child.id,
    )
    updated = h.sessions.update_terminal_pickup_metadata(child.id, agent_run_id=run.id)
    assert updated is not None and updated.agent_run_id == run.id
    return updated


def _pane_ref(h: _Harness, workspace_ref: int, tab_ref: int, pane_ref: int) -> str:
    node = h.workspaces.resolve_node()
    assert node.ref is not None
    return f"{node.ref}:{workspace_ref}:{tab_ref}:{pane_ref}"


async def _raises(code: str, operation: Awaitable[object]) -> WorkspaceOpError:
    with pytest.raises(WorkspaceOpError) as caught:
        await operation
    assert caught.value.code == code, str(caught.value)
    return caught.value


async def test_split_spawns_with_pane_identity_env_and_rolls_back(harness: _Harness) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR)
    first = (await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)).panes[0]

    # The registry also serves tmux (the configured default backend); pane spawns
    # still resolve the native runtime explicitly.
    split = await h.ops.pane_split(OPERATOR, first.id, "horizontal")
    pane, tab = split.panes[0], split.tabs[0]
    node = h.workspaces.resolve_node()
    request = h.native.last_request
    assert request is not None
    assert request.cwd == h.checkout
    assert dict(request.env or {}) == {
        GOBBY_NODE_ID: node.id,
        GOBBY_NODE_REF: str(node.ref),
        GOBBY_WORKSPACE_ID: workspace.id,
        GOBBY_TAB_ID: tab.id,
        GOBBY_PANE_ID: pane.id,
        GOBBY_PANE_REF: _pane_ref(h, workspace.ref, tab.ref, pane.ref),
    }
    assert pane.terminal_id == str(request.terminal_id) and pane.owns_terminal
    terminal = h.terminals.get(str(request.terminal_id))
    assert terminal is not None and (terminal.backend, terminal.state) == ("native", "live")
    assert h.tmux.create_calls == 0
    assert layout_pane_ids(tab.layout) == [first.id, pane.id]

    # The pane row exists before the spawn runs; the sweep spares it and every
    # close op over it refuses with busy until the split replies.
    h.native.spawning.clear()
    h.native.spawn_hold = asyncio.Event()
    held = asyncio.create_task(h.ops.pane_split(OPERATOR, first.id, "vertical"))
    await h.native.spawning.wait()
    [in_flight] = [row for row in h.workspaces.list_panes(workspace.id) if row.terminal_id is None]
    assert h.workspaces.sweep_dead_panes(workspace.id) == LayoutChange()
    await _raises("busy", h.ops.pane_close(OPERATOR, in_flight.id))
    await _raises("busy", h.ops.tab_close(OPERATOR, in_flight.tab_id))
    await _raises("busy", h.ops.workspace_close(OPERATOR, workspace.id))
    h.native.spawn_hold.set()
    bound = (await held).panes[0]
    assert bound.id == in_flight.id and bound.terminal_id is not None
    h.native.spawn_hold = None

    # A spawn that fails or raises removes the row and restores the layout.
    layouts = {row.id: row.layout for row in h.workspaces.list_tabs(workspace.id)}
    panes = {row.id for row in h.workspaces.list_panes(workspace.id)}
    h.native.fail_spawn = True
    await _raises("terminal_failed", h.ops.pane_split(OPERATOR, first.id, "horizontal"))
    h.native.fail_spawn = False
    with patch(
        "gobby.terminals.workspace_ops.spawn_web_terminal", side_effect=RuntimeError("host gone")
    ):
        await _raises("terminal_failed", h.ops.pane_split(OPERATOR, first.id, "horizontal"))
    unavailable = h.build_ops(runtime_registry(h.tmux))
    await _raises("terminal_failed", unavailable.pane_split(OPERATOR, first.id, "horizontal"))
    assert {row.id: row.layout for row in h.workspaces.list_tabs(workspace.id)} == layouts
    assert {row.id for row in h.workspaces.list_panes(workspace.id)} == panes
    assert h.tmux.create_calls == 0
    assert h.events[-1]["kind"] == "pane.removed"

    # A cascade that removes the pane mid-spawn kills the minted terminal.
    doomed = (await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)).panes[0]
    h.native.spawning.clear()
    h.native.spawn_hold = asyncio.Event()
    cascaded = asyncio.create_task(h.ops.pane_split(OPERATOR, doomed.id, "horizontal"))
    await h.native.spawning.wait()
    minted = h.native.last_request
    assert minted is not None
    h.db.execute("DELETE FROM workspace_tabs WHERE id = %s", (doomed.tab_id,))
    h.native.spawn_hold.set()
    await _raises("not_found", cascaded)
    minted_id = str(minted.terminal_id)
    assert ("ht-1", minted_id) in h.native.terminated_host_ids
    killed = h.terminals.get(minted_id)
    assert killed is not None and killed.state == "exited"


@pytest.mark.parametrize("role", [None, "persistent-reviewer"])
async def test_tab_create_and_pane_split_carry_role(harness: _Harness, role: str | None) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR)
    first = (await h.ops.tab_create(OPERATOR, workspace.id, h.project_id, role=role)).panes[0]
    split = (await h.ops.pane_split(OPERATOR, first.id, "horizontal", role=role)).panes[0]
    assert first.role == split.role == role
    assert {pane.id: pane.role for pane in h.workspaces.list_panes(workspace.id)} == {
        first.id: role,
        split.id: role,
    }
    snapshot = await h.ops.workspace_snapshot(OPERATOR, workspace.id)
    snapshot_rows = _result(snapshot.panes)
    event_rows = [
        event["panes"][0] for event in h.events if event["kind"] in {"tab.created", "pane.added"}
    ]
    assert [row["id"] for row in event_rows] == [first.id, split.id]
    assert {row["id"] for row in snapshot_rows} == {first.id, split.id}
    for row in [*event_rows, *snapshot_rows]:
        if role is None:
            assert "role" not in row
        else:
            assert row["role"] == role


async def test_empty_pane_role_is_invalid_before_storage(harness: _Harness) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR)
    pane = (await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)).panes[0]
    with (
        patch.object(h.workspaces, "create_tab") as create,
        patch.object(h.workspaces, "add_pane") as split,
    ):
        await _raises("invalid_op", h.ops.tab_create(OPERATOR, workspace.id, h.project_id, role=""))
        await _raises("invalid_op", h.ops.pane_split(OPERATOR, pane.id, "horizontal", role=""))
    create.assert_not_called()
    split.assert_not_called()
    assert h.workspaces.list_panes(workspace.id) == [pane]


@pytest.mark.parametrize("method", ["tab_create", "pane_split"])
def test_workspace_ws_accepts_role_argument(method: str) -> None:
    args: dict[str, object] = {"role": "reviewer"}
    if method == "tab_create":
        args.update(workspace="w#0", project_id=str(uuid.uuid4()))
    else:
        args.update(pane="p#0:0:0", axis="horizontal")
    assert _arguments(method, args, _OP_ENVELOPE)["role"] == "reviewer"


async def test_spawned_shell_is_killed_when_pane_binding_raises(harness: _Harness) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR)
    first = (await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)).panes[0]

    with patch.object(h.workspaces, "set_pane_terminal", side_effect=RuntimeError("bind failed")):
        await _raises("terminal_failed", h.ops.pane_split(OPERATOR, first.id, "horizontal"))

    minted = h.native.last_request
    assert minted is not None
    terminal = h.terminals.get(str(minted.terminal_id))
    assert terminal is not None and terminal.state == "exited"
    assert ("ht-1", str(minted.terminal_id)) in h.native.terminated_host_ids
    assert [pane.id for pane in h.workspaces.list_panes(workspace.id)] == [first.id]


async def test_adopt_close_and_move_semantics(harness: _Harness) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR)
    agent = _live_terminal(h.terminals, h.project_id, "tmux")
    external = _live_terminal(h.terminals, h.project_id, "native")

    adopted_tab = await h.ops.tab_create(OPERATOR, workspace.id, h.project_id, terminal_id=agent.id)
    tab, agent_pane = adopted_tab.tabs[0], adopted_tab.panes[0]
    adopted_split = await h.ops.pane_split(
        OPERATOR, agent_pane.id, "vertical", terminal_id=external.id
    )
    external_pane = adopted_split.panes[0]
    assert (agent_pane.terminal_id, agent_pane.owns_terminal) == (agent.id, False)
    assert (external_pane.terminal_id, external_pane.owns_terminal) == (external.id, False)
    assert (h.native.create_calls, h.tmux.create_calls) == (0, 0)

    held_by_agent_pane = await _raises(
        "busy", h.ops.tab_create(OPERATOR, workspace.id, h.project_id, terminal_id=agent.id)
    )
    assert _pane_ref(h, workspace.ref, tab.ref, agent_pane.ref) in str(held_by_agent_pane)
    held_by_external_pane = await _raises(
        "busy", h.ops.pane_split(OPERATOR, agent_pane.id, "horizontal", terminal_id=external.id)
    )
    assert _pane_ref(h, workspace.ref, tab.ref, external_pane.ref) in str(held_by_external_pane)
    assert [row.id for row in h.workspaces.list_tabs(workspace.id)] == [tab.id]
    assert len(h.workspaces.list_panes(workspace.id)) == 2

    # The adopted tmux terminal is read and written through the tmux runtime.
    h.tmux.snapshot_text = "agent ready"
    assert (await h.ops.pane_read(OPERATOR, agent_pane.id, lines=20)).text == "agent ready"
    h.tmux.snapshot_effects = ["booting", "agent ready"]
    waited = await h.ops.pane_wait_for_output(
        OPERATOR, agent_pane.id, r"ready", timeout_seconds=5, poll_interval_seconds=0.1
    )
    assert (waited.matched, waited.reason) == (True, "matched")
    sent = await h.ops.pane_send_keys(OPERATOR, agent_pane.id, "hello\n", idempotency_key="k-1")
    assert (sent.idempotency_key, sent.indeterminate) == ("k-1", False)
    assert h.tmux.write_log == [("text", "hello\n")]
    assert h.native.write_log == []

    # pane.move across tabs collapses the source split and takes the lowest free ref.
    owned = (await h.ops.pane_split(OPERATOR, external_pane.id, "horizontal")).panes[0]
    assert owned.ref == 2
    destination = await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)
    destination_tab, destination_pane = destination.tabs[0], destination.panes[0]
    moved = await h.ops.pane_move(
        OPERATOR, owned.id, destination_tab.id, beside=destination_pane.id
    )
    tabs = {row.id: row for row in moved.tabs}
    assert layout_pane_ids(tabs[tab.id].layout) == [agent_pane.id, external_pane.id]
    assert layout_pane_ids(tabs[destination_tab.id].layout) == [destination_pane.id, owned.id]
    assert (moved.panes[0].tab_id, moved.panes[0].ref) == (destination_tab.id, 1)

    # pane.close kills an owned live terminal and releases an adopted one.
    await h.ops.pane_close(OPERATOR, owned.id)
    owned_terminal = h.terminals.get(str(owned.terminal_id))
    assert owned_terminal is not None and owned_terminal.state == "exited"
    assert ("ht-1", owned_terminal.id) in h.native.terminated_host_ids
    await h.ops.pane_close(OPERATOR, external_pane.id)
    released = h.terminals.get(external.id)
    assert released is not None and released.state == "live"
    assert all(host_epoch != "adopted" for _host, host_epoch in h.native.terminated_host_ids)

    # An exited terminal's pane row goes at once with nothing left to settle.
    exited = (await h.ops.pane_split(OPERATOR, agent_pane.id, "horizontal")).panes[0]
    assert h.terminals.mark_exited(str(exited.terminal_id)) is not None
    terminated = list(h.native.terminated_host_ids)
    await h.ops.pane_close(OPERATOR, exited.id)
    assert exited.id not in {row.id for row in h.workspaces.list_panes(workspace.id)}
    assert h.native.terminated_host_ids == terminated
    agent_row = h.terminals.get(agent.id)
    assert agent_row is not None and agent_row.state == "live"


async def test_closing_a_migrated_tab_kills_only_its_new_shell(harness: _Harness) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR)
    first = _live_terminal(h.terminals, h.project_id, "native")
    second = _live_terminal(h.terminals, h.project_id, "native")
    created = await h.ops.tab_create(OPERATOR, workspace.id, h.project_id, terminal_id=first.id)
    tab, first_pane = created.tabs[0], created.panes[0]
    second_pane = (
        await h.ops.pane_split(OPERATOR, first_pane.id, "horizontal", terminal_id=second.id)
    ).panes[0]
    await h.ops.pane_resize(OPERATOR, first_pane.id, 0.3)
    owned = (await h.ops.pane_split(OPERATOR, second_pane.id, "horizontal")).panes[0]
    assert (first_pane.owns_terminal, second_pane.owns_terminal, owned.owns_terminal) == (
        False,
        False,
        True,
    )

    await h.ops.tab_close(OPERATOR, tab.id)
    assert not h.workspaces.list_tabs(workspace.id)
    for terminal_id in (first.id, second.id):
        row = h.terminals.get(terminal_id)
        assert row is not None and row.state == "live"
    owned_row = h.terminals.get(str(owned.terminal_id))
    assert owned_row is not None and owned_row.state == "exited"
    assert h.native.create_calls == 1
    assert len(h.native.terminated_host_ids) == 1


async def test_actor_scope_guards_kill_spawn_and_adopt(harness: _Harness) -> None:
    h = harness
    other_project = LocalProjectManager(h.db).create(name="workspace-ops-outsider")
    member = _session(h, "member", h.project_id)
    lead = _session(h, "lead", h.project_id)
    lead_child = _session(h, "lead-child", other_project.id, lead)
    outsider = _session(h, "outsider", other_project.id)
    autonomous = _autonomous_session(h, member)

    workspace = await h.ops.workspace_create(OPERATOR)
    spawned = await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)
    tab, pane = spawned.tabs[0], spawned.panes[0]
    agent = _live_terminal(h.terminals, h.project_id, "tmux", session_id=lead.id)
    spawns = h.native.create_calls

    for actor in ("nobody", f"session:{outsider.id}", f"session:{autonomous.id}"):
        # Each op's coroutine is built inside its own _raises call, so a failed
        # assertion leaves no un-awaited coroutine behind.
        refused: list[Callable[[], Awaitable[object]]] = [
            partial(h.ops.pane_read, actor, pane.id),
            partial(h.ops.pane_send_text, actor, pane.id, "ls", submit=True),
            partial(h.ops.pane_send_keys, actor, pane.id, "enter", literal=False),
            partial(h.ops.pane_wait_for_output, actor, pane.id, "x", timeout_seconds=0),
            partial(h.ops.pane_split, actor, pane.id, "horizontal"),
            partial(h.ops.tab_create, actor, workspace.id, h.project_id),
            partial(h.ops.tab_create, actor, workspace.id, h.project_id, terminal_id=agent.id),
            partial(h.ops.pane_close, actor, pane.id),
            partial(h.ops.tab_close, actor, tab.id),
            partial(h.ops.workspace_close, actor, workspace.id),
        ]
        for operation in refused:
            await _raises("forbidden", operation())
    assert h.native.create_calls == spawns
    assert h.native.write_log == [] and h.native.terminated_host_ids == []
    assert [row.id for row in h.workspaces.list_panes(workspace.id)] == [pane.id]

    # Same project and agent tree are in scope; a tree member outside the project
    # adopts and reads the lead's terminal but cannot spawn into this project.
    member_actor, tree_actor = f"session:{member.id}", f"session:{lead_child.id}"
    h.native.snapshot_text = "member view"
    assert (await h.ops.pane_read(member_actor, pane.id)).text == "member view"
    adopted = (
        await h.ops.tab_create(tree_actor, workspace.id, h.project_id, terminal_id=agent.id)
    ).panes[0]
    h.tmux.snapshot_text = "lead view"
    assert (await h.ops.pane_read(tree_actor, adopted.id)).text == "lead view"
    await _raises("forbidden", h.ops.pane_split(tree_actor, adopted.id, "horizontal"))

    # Row-only ops, and releasing an adopted terminal, are open to any actor.
    for actor in (f"session:{outsider.id}", f"session:{autonomous.id}"):
        side = await h.ops.workspace_create(actor, f"side-{actor[-8:]}")
        assert (await h.ops.workspace_rename(actor, side.id, f"renamed-{actor[-8:]}")).name == (
            f"renamed-{actor[-8:]}"
        )
        assert (await h.ops.tab_rename(actor, tab.id, "scoped")).title == "scoped"
        assert (await h.ops.pane_rename(actor, pane.id, "main")).label == "main"
        assert (await h.ops.tab_move(actor, tab.id, 1)).tabs[-1].id == tab.id
        hints, focused = await h.ops.workspace_set_focus_hints(
            actor, workspace.id, project_id=h.project_id, tab=tab.id, pane=pane.id
        )
        assert hints.focused_tab_id == tab.id
        assert focused is not None and focused.focused_pane_id == pane.id
    await h.ops.pane_close(f"session:{outsider.id}", adopted.id)
    still_live = h.terminals.get(agent.id)
    assert still_live is not None and still_live.state == "live"

    # send_keys authorizes through the same policy.
    for caller, error_code in (
        (member, None),
        (lead_child, None),
        (outsider, "send_keys_target_forbidden"),
        (autonomous, "send_keys_autonomous_agent_forbidden"),
    ):
        with session_context_for_test(caller.id):
            target_id, error = _authorize_send_keys_target(lead.id, h.sessions)
        assert target_id == (lead.id if error_code is None else None)
        assert (error or {}).get("error_code") == error_code


async def test_ops_publish_events_and_raise_typed_errors(harness: _Harness) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR, "events")
    created = await h.ops.tab_create(OPERATOR, workspace.id, h.project_id, title="one")
    first_tab, first = created.tabs[0], created.panes[0]
    second = (await h.ops.pane_split(OPERATOR, first.id, "horizontal")).panes[0]
    await h.ops.pane_swap(OPERATOR, first.id, second.id)
    await h.ops.pane_resize(OPERATOR, second.id, 0.3)
    await h.ops.pane_rename(OPERATOR, first.id, "main")
    await h.ops.tab_rename(OPERATOR, first_tab.id, "renamed")
    other = await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)
    other_tab, other_pane = other.tabs[0], other.panes[0]
    await h.ops.tab_move(OPERATOR, other_tab.id, 0)
    await h.ops.pane_move(OPERATOR, second.id, other_tab.id, beside=other_pane.id)
    await h.ops.workspace_rename(OPERATOR, workspace.id, "renamed")
    await h.ops.workspace_set_focus_hints(
        OPERATOR, workspace.id, project_id=h.project_id, tab=other_tab.id, pane=other_pane.id
    )
    await h.ops.pane_close(OPERATOR, second.id)
    assert h.terminals.mark_exited(str(first.terminal_id)) is not None
    await h.ops.pane_rename(OPERATOR, other_pane.id, "survivor")
    elsewhere = await h.ops.workspace_create(OPERATOR, "elsewhere")
    await h.ops.tab_move(OPERATOR, other_tab.id, 0, workspace=elsewhere.id)
    await h.ops.tab_close(OPERATOR, other_tab.id)
    await h.ops.workspace_close(OPERATOR, workspace.id)

    home, away = workspace.id, elsewhere.id
    assert [(event["kind"], event["workspace_id"]) for event in h.events] == [
        ("workspace.created", home),
        ("tab.created", home),
        ("pane.added", home),
        ("pane.swapped", home),
        ("pane.resized", home),
        ("pane.renamed", home),
        ("tab.renamed", home),
        ("tab.created", home),
        ("tab.moved", home),
        ("pane.moved", home),
        ("workspace.renamed", home),
        ("focus_hints", home),
        ("pane.removed", home),
        ("pane.removed", home),
        ("tab.removed", home),
        ("pane.renamed", home),
        ("workspace.created", away),
        ("tab.moved", away),
        ("tab.moved", home),
        ("tab.closed", away),
        ("workspace.closed", home),
    ]
    events = h.events
    assert [row["id"] for row in events[1]["tabs"]] == [first_tab.id]
    assert [row["terminal_id"] for row in events[1]["panes"]] == [first.terminal_id]
    assert [row["id"] for row in events[12]["panes"]] == [second.id]
    assert [row["id"] for row in events[13]["panes"]] == [first.id]
    assert [row["id"] for row in events[14]["tabs"]] == [first_tab.id]
    assert events[-1]["workspace"] is not None and events[-1]["workspace"]["id"] == home

    # Every failure is typed; a refused op publishes nothing.
    h.events.clear()
    live = await h.ops.workspace_create(OPERATOR)
    pane = (await h.ops.tab_create(OPERATOR, live.id, h.project_id)).panes[0]
    published = len(h.events)
    outsider = _session(h, "typed-outsider", LocalProjectManager(h.db).create(name="typed").id)
    failures = [
        await _raises("not_found", h.ops.pane_rename(OPERATOR, str(uuid.uuid4()), "x")),
        await _raises("invalid_ref", h.ops.pane_rename(OPERATOR, "0:0:0:bogus", "x")),
        await _raises("invalid_ref", h.ops.pane_rename(OPERATOR, pane.tab_id, "x")),
        await _raises("invalid_op", h.ops.pane_split(OPERATOR, pane.id, "diagonal")),
        await _raises(
            "invalid_op", h.ops.pane_wait_for_output(OPERATOR, pane.id, "(", timeout_seconds=0)
        ),
        await _raises(
            "busy",
            h.ops.tab_create(OPERATOR, live.id, h.project_id, terminal_id=str(pane.terminal_id)),
        ),
        await _raises("forbidden", h.ops.pane_read(f"session:{outsider.id}", pane.id)),
    ]
    assert len(h.events) == published
    h.native.fail_spawn = True
    failures.append(
        await _raises("terminal_failed", h.ops.tab_create(OPERATOR, live.id, h.project_id))
    )
    assert [event["kind"] for event in h.events[published:]] == ["pane.removed", "tab.removed"]
    assert {failure.code for failure in failures} == ERROR_CODES


async def test_split_spawn_timeout_rolls_back_and_leaves_workspace_closable(
    harness: _Harness,
) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR)
    first = (await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)).panes[0]
    panes = [row.id for row in h.workspaces.list_panes(workspace.id)]

    # A wedged host never answers the spawn; the bounded wait fails the split.
    h.native.spawn_hold = asyncio.Event()
    with patch("gobby.terminals.workspace_ops.PANE_SPAWN_TIMEOUT_SECONDS", 0.05):
        wedged = await _raises(
            "terminal_failed",
            asyncio.wait_for(h.ops.pane_split(OPERATOR, first.id, "horizontal"), timeout=1.0),
        )
    assert "spawn timed out" in str(wedged)
    minted = h.native.last_request
    assert minted is not None
    assert [row.id for row in h.workspaces.list_panes(workspace.id)] == panes
    assert h.events[-1]["kind"] == "pane.removed"

    # The rolled-back pane is no longer in flight, so the workspace closes.
    closed = await h.ops.workspace_close(OPERATOR, workspace.id)
    assert closed.id == workspace.id

    # Once the host answers, the timeout cleanup kills the late spawn.
    h.native.terminate_host_started.clear()
    h.native.spawn_hold.set()
    await asyncio.wait_for(h.native.terminate_host_started.wait(), timeout=1.0)
    assert ("ht-1", str(minted.terminal_id)) in h.native.terminated_host_ids


async def test_close_ops_kill_owned_terminals_and_release_adopted_ones(
    harness: _Harness,
) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR)
    external = _live_terminal(h.terminals, h.project_id, "native")
    agent = _live_terminal(h.terminals, h.project_id, "tmux")

    closing = await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)
    first = closing.panes[0]
    second = (await h.ops.pane_split(OPERATOR, first.id, "horizontal")).panes[0]
    await h.ops.pane_split(OPERATOR, first.id, "vertical", terminal_id=external.id)
    await h.ops.workspace_set_focus_hints(
        OPERATOR, workspace.id, project_id=h.project_id, tab=closing.tabs[0].id, pane=first.id
    )
    await h.ops.tab_close(OPERATOR, closing.tabs[0].id)
    unseeded = h.workspaces.get(workspace.id)
    assert unseeded is not None and unseeded.focused_tab_id is None

    kept = await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)
    third = kept.panes[0]
    await h.ops.pane_split(OPERATOR, third.id, "horizontal", terminal_id=agent.id)
    await h.ops.workspace_close(OPERATOR, workspace.id)

    for owned_pane in (first, second, third):
        owned = h.terminals.get(str(owned_pane.terminal_id))
        assert owned is not None and owned.state == "exited"
        assert ("ht-1", owned.id) in h.native.terminated_host_ids
    for adopted in (external, agent):
        released = h.terminals.get(adopted.id)
        assert released is not None and released.state == "live"
    assert all(host_epoch != "adopted" for _host, host_epoch in h.native.terminated_host_ids)
    assert h.tmux.killed_ids == set()

    # A kill that fails after the rows are gone orphans the terminal, so it stays
    # listed and killable instead of live behind no pane.
    retry = await h.ops.workspace_create(OPERATOR, "retry")
    failing = (await h.ops.tab_create(OPERATOR, retry.id, h.project_id)).panes[0]
    h.native.terminate_host_failures = [ConnectionError("gterm host unavailable")]
    await h.ops.pane_close(OPERATOR, failing.id)
    assert h.workspaces.list_panes(retry.id) == []
    orphaned = h.terminals.get(str(failing.terminal_id))
    assert orphaned is not None and orphaned.state == "orphaned"


async def test_cross_workspace_pane_move_publishes_each_workspace_its_own_rows(
    harness: _Harness,
) -> None:
    h = harness
    home = await h.ops.workspace_create(OPERATOR, "home")
    away = await h.ops.workspace_create(OPERATOR, "away")
    staying = (await h.ops.tab_create(OPERATOR, home.id, h.project_id)).panes[0]
    moving = (await h.ops.pane_split(OPERATOR, staying.id, "horizontal")).panes[0]
    destination = await h.ops.tab_create(OPERATOR, away.id, h.project_id)
    away_tab, away_pane = destination.tabs[0], destination.panes[0]

    def published() -> list[tuple[str, str, list[str], list[str]]]:
        return [
            (
                event["kind"],
                event["workspace_id"],
                [row["id"] for row in event["tabs"]],
                [row["id"] for row in event["panes"]],
            )
            for event in h.events
        ]

    h.events.clear()
    await h.ops.pane_move(OPERATOR, moving.id, away_tab.id, beside=away_pane.id)
    assert published() == [
        ("pane.moved", away.id, [away_tab.id], [moving.id]),
        ("pane.moved", home.id, [staying.tab_id], [moving.id]),
    ]

    # Moving the last pane away removes the emptied tab from its workspace only.
    h.events.clear()
    await h.ops.pane_move(OPERATOR, staying.id, away_tab.id, beside=moving.id)
    assert published() == [
        ("pane.moved", away.id, [away_tab.id], [staying.id]),
        ("pane.moved", home.id, [], [staying.id]),
        ("tab.removed", home.id, [staying.tab_id], []),
    ]


async def test_workspace_create_announces_only_a_new_row(harness: _Harness) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR, "announced")
    assert [event["kind"] for event in h.events] == ["workspace.created"]
    pane = (await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)).panes[0]
    assert h.terminals.mark_exited(str(pane.terminal_id)) is not None

    h.events.clear()
    existing = await h.ops.workspace_create(OPERATOR, "announced")
    assert existing.id == workspace.id
    assert [event["kind"] for event in h.events] == ["pane.removed", "tab.removed"]


async def test_project_snapshot_resolves_one_workspace_and_keeps_the_tab(
    harness: _Harness,
) -> None:
    h = harness
    scratch = await h.ops.workspace_snapshot(OPERATOR)
    assert scratch.workspace.default_project_id is None
    opened = await h.ops.workspace_snapshot(OPERATOR, project_id=h.project_id)
    assert opened.workspace.id != scratch.workspace.id
    assert opened.workspace.default_project_id == h.project_id
    assert opened.workspace.ref == scratch.workspace.ref + 1
    again = await h.ops.workspace_snapshot(OPERATOR, project_id=h.project_id)
    assert again.workspace.id == opened.workspace.id
    explicit = await h.ops.workspace_snapshot(
        OPERATOR, scratch.workspace.id, project_id=h.project_id
    )
    assert explicit.workspace.id == scratch.workspace.id
    change = await h.ops.tab_create(OPERATOR, opened.workspace.id, h.project_id)
    tab, pane = change.tabs[0], change.panes[0]
    assert tab.project_id == h.project_id
    assert tab.worktree_id is None
    assert pane.ref == 0
    listed = await h.ops.workspace_list(OPERATOR)
    assert [row.id for row in listed] == [scratch.workspace.id, opened.workspace.id]
    assert opened.workspace.to_dict()["default_project_id"] == h.project_id


async def test_rollback_retries_a_concurrent_move_and_types_storage_failures(
    harness: _Harness,
) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR)
    first = (await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)).panes[0]
    panes = [row.id for row in h.workspaces.list_panes(workspace.id)]
    remove_pane = h.workspaces.remove_pane
    attempts: list[str] = []

    def moved_once(pane_id: str) -> LayoutChange:
        attempts.append(pane_id)
        if len(attempts) == 1:
            raise WorkspaceNotFoundError("A pane moved to another tab concurrently; retry")
        return remove_pane(pane_id)

    h.native.fail_spawn = True
    with patch.object(h.workspaces, "remove_pane", side_effect=moved_once):
        await _raises("terminal_failed", h.ops.pane_split(OPERATOR, first.id, "horizontal"))
    assert len(attempts) == 2 and len(set(attempts)) == 1
    assert [row.id for row in h.workspaces.list_panes(workspace.id)] == panes

    # A storage failure during the rollback is still a typed terminal_failed; the
    # unbound pane it leaves is no longer in flight, so the next op's sweep prunes it.
    with patch.object(h.workspaces, "remove_pane", side_effect=OperationalError("db gone")):
        lost = await _raises("terminal_failed", h.ops.pane_split(OPERATOR, first.id, "horizontal"))
    assert "db gone" in str(lost)
    assert len(h.workspaces.list_panes(workspace.id)) == len(panes) + 1
    await h.ops.pane_rename(OPERATOR, first.id, "main")
    assert [row.id for row in h.workspaces.list_panes(workspace.id)] == panes


async def test_remote_workspace_refuses_mutations_and_is_never_swept(harness: _Harness) -> None:
    h = harness
    remote = LocalMachineManager(h.db).upsert_seen(
        REMOTE_MACHINE_ID, TEST_USER_ID, hostname="remote-node"
    )
    assert remote.ref is not None
    workspace, _created = h.workspaces.create(remote.id, "remote")
    # The remote node's split is mid-spawn: the pane has no terminal yet and only
    # that node's daemon holds it in flight.
    spawning = h.workspaces.create_tab(
        workspace.id, pane_id=str(uuid.uuid4()), project_id=h.project_id
    )
    tab, pane = spawning.tabs[0], spawning.panes[0]
    local = await h.ops.workspace_create(OPERATOR)
    local_created = await h.ops.tab_create(OPERATOR, local.id, h.project_id)
    local_tab, local_pane = local_created.tabs[0], local_created.panes[0]
    h.events.clear()
    spawns = h.native.create_calls

    refused: list[Callable[[], Awaitable[object]]] = [
        partial(h.ops.workspace_create, OPERATOR, "remote", node="remote-node"),
        partial(h.ops.workspace_rename, OPERATOR, f"{remote.ref}:{workspace.ref}", "renamed"),
        partial(h.ops.workspace_close, OPERATOR, workspace.id),
        partial(
            h.ops.workspace_set_focus_hints,
            OPERATOR,
            workspace.id,
            project_id=h.project_id,
            tab=tab.id,
            pane=pane.id,
        ),
        partial(h.ops.tab_create, OPERATOR, workspace.id, h.project_id),
        partial(h.ops.tab_rename, OPERATOR, tab.id, "renamed"),
        partial(h.ops.tab_move, OPERATOR, tab.id, 0),
        partial(h.ops.tab_close, OPERATOR, tab.id),
        partial(h.ops.pane_split, OPERATOR, pane.id, "horizontal"),
        partial(h.ops.pane_swap, OPERATOR, pane.id, pane.id),
        partial(h.ops.pane_resize, OPERATOR, pane.id, 0.4),
        partial(h.ops.pane_rename, OPERATOR, pane.id, "renamed"),
        partial(h.ops.pane_close, OPERATOR, pane.id),
        partial(h.ops.pane_read, OPERATOR, pane.id),
        partial(h.ops.pane_send_text, OPERATOR, pane.id, "ls"),
        # A local row cannot be moved into another node's workspace either.
        partial(h.ops.pane_move, OPERATOR, local_pane.id, tab.id),
        partial(h.ops.tab_move, OPERATOR, local_tab.id, 0, workspace=workspace.id),
    ]
    for operation in refused:
        refusal = await _raises("invalid_op", operation())
        assert f"node {remote.ref}" in str(refusal)
    assert [row.id for row in h.workspaces.list_panes(workspace.id)] == [pane.id]
    assert [row.id for row in h.workspaces.list_tabs(local.id)] == [local_tab.id]
    assert h.native.create_calls == spawns
    assert h.events == []


async def test_worktree_tabs_and_splits_spawn_in_the_worktree_checkout(
    harness: _Harness,
) -> None:
    h = harness
    worktrees = LocalWorktreeManager(h.db)
    worktree = worktrees.create(h.project_id, "workspace-ops", "/private/tmp/workspace-ops-wt")
    workspace = await h.ops.workspace_create(OPERATOR)

    created = await h.ops.tab_create(OPERATOR, workspace.id, h.project_id, worktree_id=worktree.id)
    assert created.tabs[0].worktree_id == worktree.id
    tab_request = h.native.last_request
    assert tab_request is not None and tab_request.cwd == worktree.worktree_path
    await h.ops.pane_split(OPERATOR, created.panes[0].id, "vertical")
    split_request = h.native.last_request
    assert split_request is not None and split_request is not tab_request
    assert split_request.cwd == worktree.worktree_path

    # An unknown worktree, or one of another project, is not_found before any row or spawn.
    outsider = LocalProjectManager(h.db).create(name="workspace-ops-worktree-owner")
    foreign = worktrees.create(outsider.id, "foreign", "/private/tmp/workspace-ops-foreign")
    spawns, panes = h.native.create_calls, len(h.workspaces.list_panes(workspace.id))
    for worktree_id in (str(uuid.uuid4()), foreign.id):
        await _raises(
            "not_found",
            h.ops.tab_create(OPERATOR, workspace.id, h.project_id, worktree_id=worktree_id),
        )
    assert h.native.create_calls == spawns
    assert len(h.workspaces.list_panes(workspace.id)) == panes


async def test_personal_workspace_shell_uses_operator_launch_directory(
    harness: _Harness, tmp_path: Path
) -> None:
    h = harness
    h.db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (PERSONAL_PROJECT_ID, "_personal"),
    )
    workspace = await h.ops.workspace_create(OPERATOR)
    launch_dir = str(tmp_path)
    await _raises(
        "invalid_op",
        h.ops.tab_create(OPERATOR, workspace.id, h.project_id, cwd=launch_dir),
    )
    await _raises(
        "invalid_op",
        h.ops.tab_create(OPERATOR, workspace.id, PERSONAL_PROJECT_ID, cwd="relative/path"),
    )
    await _raises(
        "invalid_op",
        h.ops.tab_create(
            OPERATOR, workspace.id, PERSONAL_PROJECT_ID, worktree_id="foreign", cwd=launch_dir
        ),
    )

    created = await h.ops.tab_create(OPERATOR, workspace.id, PERSONAL_PROJECT_ID, cwd=launch_dir)
    first = created.panes[0]
    assert first.terminal_id is not None and first.owns_terminal
    assert h.native.last_request is not None and h.native.last_request.cwd == launch_dir
    split = await h.ops.pane_split(OPERATOR, first.id, "horizontal", cwd=launch_dir)
    second = split.panes[0]
    assert second.terminal_id is not None and second.owns_terminal
    assert h.native.last_request is not None and h.native.last_request.cwd == launch_dir

    await h.ops.workspace_close(OPERATOR, workspace.id)
    for terminal_id in (first.terminal_id, second.terminal_id):
        terminal = h.terminals.get(terminal_id)
        assert terminal is not None and terminal.state == "exited"


async def test_adopt_race_on_the_unique_index_raises_busy(harness: _Harness) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR)
    agent = _live_terminal(h.terminals, h.project_id, "tmux")
    held = await h.ops.tab_create(OPERATOR, workspace.id, h.project_id, terminal_id=agent.id)
    holder_tab, holder = held.tabs[0], held.panes[0]
    layouts = {row.id: row.layout for row in h.workspaces.list_tabs(workspace.id)}

    # The adopt check sees no holder, as if the winning adopt had not committed yet,
    # so the partial unique index is what refuses the second binding.
    winner = h.workspaces.get_pane_for_terminal(agent.id)
    with patch.object(h.workspaces, "get_pane_for_terminal", side_effect=[None, winner]):
        raced = await _raises(
            "busy", h.ops.pane_split(OPERATOR, holder.id, "horizontal", terminal_id=agent.id)
        )
    assert _pane_ref(h, workspace.ref, holder_tab.ref, holder.ref) in str(raced)
    assert [row.id for row in h.workspaces.list_panes(workspace.id)] == [holder.id]
    assert {row.id: row.layout for row in h.workspaces.list_tabs(workspace.id)} == layouts
    assert h.events[-1]["kind"] == "pane.removed"


async def test_send_keys_scope_without_a_caller_is_caller_not_found(harness: _Harness) -> None:
    h = harness
    caller = _session(h, "scopeless", h.project_id)
    with (
        session_context_for_test(caller.id),
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal_send_keys.resolve_actor_scope",
            return_value=ActorScope(h.sessions),
        ),
    ):
        target_id, error = _authorize_send_keys_target(caller.id, h.sessions)
    assert target_id is None
    assert (error or {}).get("error_code") == "send_keys_caller_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "frame_template", "readable"),
    [
        pytest.param("claude", "────────\n❯ {text}\n────────", True, id="claude"),
        pytest.param("codex", "────────\n❯ {text}\n────────", True, id="codex"),
        pytest.param("droid", "╭────────╮\n│ > {text} │\n╰────────╯", True, id="droid"),
    ],
)
async def test_workspace_send_text_submit_reports_unsubmitted_codex_draft(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    frame_template: str,
    readable: bool,
) -> None:
    h = harness
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_HELD_RETRY_SECONDS", 0.0)
    sync_bundled_detection_manifests(h.db)
    session = h.sessions.register(
        external_id=f"workspace-ops-{source}",
        machine_id=LOCAL_MACHINE_ID,
        source=source,
        project_id=h.project_id,
    )
    terminal = _live_terminal(h.terminals, h.project_id, "native", session_id=session.id)
    workspace = await h.ops.workspace_create(OPERATOR, "codex-submit")
    pane = (
        await h.ops.tab_create(
            OPERATOR,
            workspace.id,
            h.project_id,
            terminal_id=terminal.id,
        )
    ).panes[0]
    text = "Start the persistent Codex role and report ready."
    h.native.snapshot_text = frame_template.format(text=text)
    detector = IdleDetector(DetectionManifestRegistry(h.db), source)
    assert detector.reads_composer() is True
    read = detector.composer_read(h.native.snapshot_text)
    assert read.state == "draft"
    expected_line = text if readable else None
    assert read.line == expected_line

    result = await h.ops.pane_send_text(OPERATOR, pane.id, text, submit=True)

    assert result.indeterminate is True, (result, h.native.write_log, h.native.snapshot_modes)
    assert result.detail is not None
    assert "not submitted" in result.detail
    assert h.native.write_log == [("text", f"{text}\n"), ("key", "enter")]


@pytest.mark.asyncio
async def test_workspace_send_text_submit_retry_does_not_retype_held_draft(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrying an indeterminate verified submit sends Enter again and does not retype."""
    h = harness
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_HELD_RETRY_SECONDS", 0.0)
    sync_bundled_detection_manifests(h.db)
    session = h.sessions.register(
        external_id="workspace-ops-retry-held",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=h.project_id,
    )
    terminal = _live_terminal(h.terminals, h.project_id, "native", session_id=session.id)
    workspace = await h.ops.workspace_create(OPERATOR, "retry-held")
    pane = (
        await h.ops.tab_create(
            OPERATOR,
            workspace.id,
            h.project_id,
            terminal_id=terminal.id,
        )
    ).panes[0]
    text = "Start the persistent Codex role and report ready."
    h.native.snapshot_text = f"────────\n❯ {text}\n────────"
    key = "retry-held-draft"

    first = await h.ops.pane_send_text(OPERATOR, pane.id, text, submit=True, idempotency_key=key)
    second = await h.ops.pane_send_text(OPERATOR, pane.id, text, submit=True, idempotency_key=key)

    assert first.indeterminate is True
    assert second.indeterminate is True
    assert h.native.write_log == [
        ("text", f"{text}\n"),
        ("key", "enter"),
        ("key", "enter"),
    ]
    text_writes = sum(kind == "text" for kind, _payload in h.native.write_log)
    print(f"held retry text_writes={text_writes} write_log={h.native.write_log}")


@pytest.mark.asyncio
async def test_workspace_send_text_unreadable_submit_retries_without_retyping(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable composer after Enter is indeterminate, and a retry never retypes (#23188)."""
    h = harness
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_HELD_RETRY_SECONDS", 0.0)
    sync_bundled_detection_manifests(h.db)
    session = h.sessions.register(
        external_id="workspace-ops-retry-unreadable",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=h.project_id,
    )
    terminal = _live_terminal(h.terminals, h.project_id, "native", session_id=session.id)
    workspace = await h.ops.workspace_create(OPERATOR, "retry-unreadable")
    pane = (
        await h.ops.tab_create(
            OPERATOR,
            workspace.id,
            h.project_id,
            terminal_id=terminal.id,
        )
    ).panes[0]
    text = "Start the persistent Codex role and report ready."
    h.native.snapshot_text = "loading"
    key = "retry-unreadable"

    first = await h.ops.pane_send_text(OPERATOR, pane.id, text, submit=True, idempotency_key=key)
    second = await h.ops.pane_send_text(OPERATOR, pane.id, text, submit=True, idempotency_key=key)

    assert first.indeterminate is True
    assert second.indeterminate is True
    assert sum(kind == "text" for kind, _payload in h.native.write_log) == 1


@pytest.mark.asyncio
async def test_workspace_send_text_submit_success_keeps_no_retry_record(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified submit drops its retry record, so the same key writes again."""
    h = harness
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_HELD_RETRY_SECONDS", 0.0)
    sync_bundled_detection_manifests(h.db)
    session = h.sessions.register(
        external_id="workspace-ops-retry-left",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=h.project_id,
    )
    terminal = _live_terminal(h.terminals, h.project_id, "native", session_id=session.id)
    workspace = await h.ops.workspace_create(OPERATOR, "retry-left")
    pane = (
        await h.ops.tab_create(
            OPERATOR,
            workspace.id,
            h.project_id,
            terminal_id=terminal.id,
        )
    ).panes[0]
    text = "Start the persistent Codex role and report ready."
    h.native.snapshot_text = "────────\n❯ other prompt\n────────"
    key = "retry-left-draft"

    first = await h.ops.pane_send_text(OPERATOR, pane.id, text, submit=True, idempotency_key=key)
    second = await h.ops.pane_send_text(OPERATOR, pane.id, text, submit=True, idempotency_key=key)

    assert first.indeterminate is False
    assert all(
        idem != key for records in _verified_submits.values() for _terminal_id, idem in records
    )
    assert second.indeterminate is False
    assert h.native.write_log == [
        ("text", f"{text}\n"),
        ("key", "enter"),
        ("text", f"{text}\n"),
        ("key", "enter"),
    ]
    text_writes = sum(kind == "text" for kind, _payload in h.native.write_log)
    print(f"success drops record text_writes={text_writes} write_log={h.native.write_log}")


@pytest.mark.asyncio
async def test_workspace_send_text_submit_bounds_held_retry_records(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Held retry records stay bounded, and an evicted key is typed again."""
    h = harness
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_HELD_RETRY_SECONDS", 0.0)
    monkeypatch.setattr("gobby.terminals.pane_io._HELD_VERIFIED_SUBMIT_LIMIT", 2)
    sync_bundled_detection_manifests(h.db)
    session = h.sessions.register(
        external_id="workspace-ops-retry-bound",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=h.project_id,
    )
    terminal = _live_terminal(h.terminals, h.project_id, "native", session_id=session.id)
    workspace = await h.ops.workspace_create(OPERATOR, "retry-bound")
    pane = (
        await h.ops.tab_create(
            OPERATOR,
            workspace.id,
            h.project_id,
            terminal_id=terminal.id,
        )
    ).panes[0]
    text = "Start the persistent Codex role and report ready."
    h.native.snapshot_text = f"────────\n❯ {text}\n────────"
    keys = ("held-a", "held-b", "held-c")
    for idempotency_key in keys:
        result = await h.ops.pane_send_text(
            OPERATOR, pane.id, text, submit=True, idempotency_key=idempotency_key
        )
        assert result.indeterminate is True
    stored = {
        idem
        for records in _verified_submits.values()
        for _terminal_id, idem in records
        if idem in keys
    }
    assert stored == {"held-b", "held-c"}
    h.native.write_log.clear()
    retried = await h.ops.pane_send_text(
        OPERATOR, pane.id, text, submit=True, idempotency_key="held-a"
    )
    assert retried.indeterminate is True
    assert ("text", f"{text}\n") in h.native.write_log


@pytest.mark.asyncio
async def test_workspace_send_text_submit_retry_after_indeterminate_enter_does_not_retype(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An indeterminate Enter leaves the text delivered; the retry does not retype it."""
    h = harness
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_HELD_RETRY_SECONDS", 0.0)
    sync_bundled_detection_manifests(h.db)
    session = h.sessions.register(
        external_id="workspace-ops-retry-enter",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=h.project_id,
    )
    terminal = _live_terminal(h.terminals, h.project_id, "native", session_id=session.id)
    workspace = await h.ops.workspace_create(OPERATOR, "retry-enter")
    pane = (
        await h.ops.tab_create(
            OPERATOR,
            workspace.id,
            h.project_id,
            terminal_id=terminal.id,
        )
    ).panes[0]
    text = "Start the persistent Codex role and report ready."
    h.native.snapshot_text = f"────────\n❯ {text}\n────────"
    h.native.outcomes = [Delivered(), IndeterminateWrite("enter reply lost")]
    key = "retry-indeterminate-enter"

    first = await h.ops.pane_send_text(OPERATOR, pane.id, text, submit=True, idempotency_key=key)
    second = await h.ops.pane_send_text(OPERATOR, pane.id, text, submit=True, idempotency_key=key)

    assert first.indeterminate is True
    assert second.indeterminate is True
    assert h.native.write_log == [
        ("text", f"{text}\n"),
        ("key", "enter"),
        ("key", "enter"),
    ]
    text_writes = sum(kind == "text" for kind, _payload in h.native.write_log)
    print(f"indeterminate enter retry text_writes={text_writes} write_log={h.native.write_log}")


@pytest.mark.asyncio
async def test_workspace_send_text_submit_retry_rejects_a_different_payload(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same idempotency key cannot retry a different payload."""
    h = harness
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_HELD_RETRY_SECONDS", 0.0)
    sync_bundled_detection_manifests(h.db)
    session = h.sessions.register(
        external_id="workspace-ops-retry-conflict",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=h.project_id,
    )
    terminal = _live_terminal(h.terminals, h.project_id, "native", session_id=session.id)
    workspace = await h.ops.workspace_create(OPERATOR, "retry-conflict")
    pane = (
        await h.ops.tab_create(
            OPERATOR,
            workspace.id,
            h.project_id,
            terminal_id=terminal.id,
        )
    ).panes[0]
    text = "Start the persistent Codex role and report ready."
    h.native.snapshot_text = f"────────\n❯ {text}\n────────"
    key = "retry-payload-conflict"

    first = await h.ops.pane_send_text(OPERATOR, pane.id, text, submit=True, idempotency_key=key)
    assert first.indeterminate is True

    await _raises(
        "invalid_op",
        h.ops.pane_send_text(
            OPERATOR,
            pane.id,
            "A different instruction.",
            submit=True,
            idempotency_key=key,
        ),
    )
    assert h.native.write_log == [("text", f"{text}\n"), ("key", "enter")]


@pytest.mark.asyncio
async def test_workspace_list_reads_storage_off_the_event_loop() -> None:
    """A workspace list must not read node storage on the event-loop thread."""
    loop_thread = threading.get_ident()
    seen: list[int] = []
    machine = SimpleNamespace(id="machine-1", ref=None, hostname="local")

    def resolve_node(_node: object) -> SimpleNamespace:
        seen.append(threading.get_ident())
        return machine

    workspaces = MagicMock()
    workspaces.resolve_node.side_effect = resolve_node
    workspaces.list_for_node.return_value = ()
    ops = WorkspaceOps(
        workspaces=workspaces,
        terminals=MagicMock(),
        registry=MagicMock(),
        coordinator=MagicMock(),
        sessions=MagicMock(),
        publish=MagicMock(),
    )
    with patch("gobby.terminals.workspace_contract.require_machine_id", return_value="machine-1"):
        listed = await ops.workspace_list("operator")
    assert listed == ()
    assert seen
    assert seen[0] != loop_thread


@pytest.mark.asyncio
async def test_workspace_rename_writes_storage_off_the_event_loop() -> None:
    """Renaming a workspace must not write the row on the event-loop thread."""
    loop_thread = threading.get_ident()
    seen: list[int] = []
    workspace = SimpleNamespace(id="ws-1")
    target = SimpleNamespace(tab=None, workspace=workspace)

    def rename(workspace_id: str, name: str) -> SimpleNamespace:
        seen.append(threading.get_ident())
        return SimpleNamespace(id=workspace_id, name=name)

    workspaces = MagicMock()
    workspaces.rename.side_effect = rename
    ops = WorkspaceOps(
        workspaces=workspaces,
        terminals=MagicMock(),
        registry=MagicMock(),
        coordinator=MagicMock(),
        sessions=MagicMock(),
        publish=MagicMock(),
    )

    async def enter(_reference: str, _node: str | None) -> SimpleNamespace:
        return target

    async def emit(*_args: object, **_kwargs: object) -> None:
        return None

    object.__setattr__(ops, "_enter", enter)
    object.__setattr__(ops, "_emit", emit)
    await ops.workspace_rename("operator", "ws-1", "renamed")
    assert seen
    assert seen[0] != loop_thread


def test_snapshot_uses_one_pool_acquisition_for_resolution_and_reads(harness: _Harness) -> None:
    import time

    from gobby.telemetry.query_timing import observe_queries

    h = harness
    workspace, _created = h.workspaces.create(LOCAL_MACHINE_ID, "snapshot")
    change = h.workspaces.create_tab(
        workspace.id, pane_id=str(uuid.uuid4()), project_id=h.project_id
    )
    tab, pane = change.tabs[0], change.panes[0]
    h.workspaces.mark_spawn_in_flight(pane.id)
    queries: list[float] = []
    acquisitions: list[float] = []
    started = time.perf_counter()
    with observe_queries(queries.append, pool_acquire_observer=acquisitions.append):
        snapshot, swept = h.ops._snapshot_storage(workspace.id, None, {})
    elapsed = time.perf_counter() - started
    print(
        f"snapshot elapsed_ms={elapsed * 1000:.1f} queries={len(queries)} "
        f"pool_acquires={len(acquisitions)} pool_wait_ms={sum(acquisitions) * 1000:.1f}"
    )
    assert snapshot.workspace == workspace
    assert snapshot.tabs == (tab,)
    assert snapshot.panes == (pane,)
    assert swept.removed_panes == ()
    assert len(acquisitions) == 1


def test_snapshot_holds_sweep_locks_until_both_row_lists_are_read(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from psycopg.errors import LockNotAvailable

    h = harness
    workspace, _created = h.workspaces.create(LOCAL_MACHINE_ID, "snapshot")
    change = h.workspaces.create_tab(
        workspace.id, pane_id=str(uuid.uuid4()), project_id=h.project_id
    )
    tab, pane = change.tabs[0], change.panes[0]
    h.workspaces.mark_spawn_in_flight(pane.id)
    read_panes = h.workspaces.list_panes
    updates: list[bool] = []

    def update_layout() -> bool:
        try:
            with h.db.transaction() as conn:
                conn.execute("SET LOCAL lock_timeout = '200ms'")
                conn.execute(
                    "UPDATE workspace_tabs SET title = 'concurrent' WHERE id = %s", (tab.id,)
                )
            return True
        except LockNotAvailable:
            return False

    with ThreadPoolExecutor(max_workers=1) as executor:

        def list_panes(workspace_id: str) -> list[WorkspacePane]:
            updates.append(executor.submit(update_layout).result(timeout=3))
            return read_panes(workspace_id)

        monkeypatch.setattr(h.workspaces, "list_panes", list_panes)
        snapshot, _swept = h.ops._snapshot_storage(workspace.id, None, {})

    assert updates == [False]
    assert snapshot.tabs == (tab,)
    assert snapshot.panes == (pane,)
    assert update_layout() is True
    assert h.workspaces.list_tabs(workspace.id)[0].title == "concurrent"


@pytest.mark.parametrize("operation", ["close_tab", "remove_pane", "move_pane"])
def test_focused_tab_deletion_and_snapshot_share_parent_first_lock_order(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    operation: Literal["close_tab", "remove_pane", "move_pane"],
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from gobby.storage import workspaces as storage
    from gobby.storage.hub.protocol import Row, Transaction
    from gobby.terminals.workspace_contract import WorkspaceSnapshot

    h = harness
    workspace, _created = h.workspaces.create(LOCAL_MACHINE_ID, "focused")
    change = h.workspaces.create_tab(
        workspace.id, pane_id=str(uuid.uuid4()), project_id=h.project_id
    )
    tab, pane = change.tabs[0], change.panes[0]
    h.workspaces.mark_spawn_in_flight(pane.id)
    h.workspaces.set_focus_hints(
        workspace.id, project_id=h.project_id, tab_id=tab.id, pane_id=pane.id
    )
    target_id = tab.id
    if operation == "move_pane":
        other, _created = h.workspaces.create(LOCAL_MACHINE_ID, "destination")
        destination = h.workspaces.create_tab(
            other.id, pane_id=str(uuid.uuid4()), project_id=h.project_id
        )
        h.workspaces.mark_spawn_in_flight(destination.panes[0].id)
        target_id = destination.tabs[0].id

    role = threading.local()
    tab_locked = threading.Event()
    delete_parent_locked = threading.Event()
    snapshot_parent_requested = threading.Event()
    snapshot_parent_locked = threading.Event()
    finish_delete = threading.Event()
    lock_rows = storage._lock_rows

    def synchronized_locks(conn: Transaction, table: storage._Table, *ids: str) -> dict[str, Row]:
        if role.name == "snapshot" and table == "workspaces":
            snapshot_parent_requested.set()
        rows = lock_rows(conn, table, *ids)
        if role.name == "delete" and table == "workspaces":
            delete_parent_locked.set()
        elif role.name == "delete" and table == "workspace_tabs":
            tab_locked.set()
            assert finish_delete.wait(timeout=3), "Deletion barrier was not released"
        elif role.name == "snapshot" and table == "workspaces":
            snapshot_parent_locked.set()
        return rows

    monkeypatch.setattr(storage, "_lock_rows", synchronized_locks)

    def delete() -> LayoutChange:
        role.name = "delete"
        with h.db.transaction() as conn:
            conn.execute("SET LOCAL statement_timeout = '3s'")
            if operation == "close_tab":
                return h.workspaces.close_tab(tab.id)
            if operation == "remove_pane":
                return h.workspaces.remove_pane(pane.id)
            return h.workspaces.move_pane(pane.id, tab_id=target_id)

    def snapshot() -> WorkspaceSnapshot:
        role.name = "snapshot"
        with h.db.transaction() as conn:
            conn.execute("SET LOCAL statement_timeout = '3s'")
            return h.ops._snapshot_storage(workspace.id, None, {})[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        closing = executor.submit(delete)
        try:
            assert tab_locked.wait(timeout=3), "Deletion did not acquire its tab lock"
            reading = executor.submit(snapshot)
            assert snapshot_parent_requested.wait(timeout=3), "Snapshot did not request its parent"
            # With reversed locks, the snapshot takes the parent while deletion
            # holds the child. With parent-first locks it waits until deletion commits.
            if not delete_parent_locked.is_set():
                assert snapshot_parent_locked.wait(timeout=3), "Snapshot did not acquire its parent"
        finally:
            finish_delete.set()
        closed = closing.result(timeout=5)
        taken = reading.result(timeout=5)

    assert closed.removed_tabs[0].id == tab.id
    assert taken.workspace.focused_tab_id is None
    assert taken.tabs == ()
    assert taken.panes == ()
    assert h.workspaces.get(workspace.id) == taken.workspace


def _storage_ops(workspaces: MagicMock) -> WorkspaceOps:
    return WorkspaceOps(
        workspaces=workspaces,
        terminals=MagicMock(),
        registry=MagicMock(),
        coordinator=MagicMock(),
        sessions=MagicMock(),
        publish=MagicMock(),
    )


@pytest.mark.asyncio
async def test_workspace_create_writes_storage_off_the_event_loop() -> None:
    """Creating a workspace must not resolve or insert the row on the event-loop thread."""
    loop_thread = threading.get_ident()
    seen: list[int] = []
    machine = SimpleNamespace(id="machine-1", ref=None, hostname="local")

    def resolve_node(_node: object) -> SimpleNamespace:
        seen.append(threading.get_ident())
        return machine

    def create(_machine_id: str, _name: str) -> tuple[SimpleNamespace, bool]:
        seen.append(threading.get_ident())
        return SimpleNamespace(id="ws-1"), False

    workspaces = MagicMock()
    workspaces.resolve_node.side_effect = resolve_node
    workspaces.create.side_effect = create
    ops = _storage_ops(workspaces)

    async def sweep(_workspace_id: str) -> SimpleNamespace:
        return SimpleNamespace(removed_panes=())

    object.__setattr__(ops, "_sweep", sweep)
    with patch("gobby.terminals.workspace_contract.require_machine_id", return_value="machine-1"):
        created = await ops.workspace_create("operator", "demo")
    assert created.id == "ws-1"
    assert len(seen) == 2
    assert all(thread != loop_thread for thread in seen)


@pytest.mark.asyncio
async def test_workspace_close_writes_storage_off_the_event_loop() -> None:
    """Closing a workspace must not list or close its rows on the event-loop thread."""
    loop_thread = threading.get_ident()
    seen: list[int] = []
    workspace = SimpleNamespace(id="ws-1")
    target = SimpleNamespace(tab=None, workspace=workspace, node=SimpleNamespace(id="machine-1"))

    def list_panes(_workspace_id: str) -> tuple[object, ...]:
        seen.append(threading.get_ident())
        return ()

    def close(_workspace_id: str, **guard: object) -> SimpleNamespace:
        assert guard == {"refuse_in_flight": True, "expected_panes": {}}
        seen.append(threading.get_ident())
        return workspace

    workspaces = MagicMock()
    workspaces.list_panes.side_effect = list_panes
    workspaces.close.side_effect = close
    ops = _storage_ops(workspaces)

    async def enter(_reference: str, _node: str | None) -> SimpleNamespace:
        return target

    async def emit(*_args: object, **_kwargs: object) -> None:
        return None

    object.__setattr__(ops, "_enter", enter)
    object.__setattr__(ops, "_emit", emit)
    closed = await ops.workspace_close("operator", "ws-1")
    assert closed.id == "ws-1"
    assert len(seen) == 2
    assert all(thread != loop_thread for thread in seen)


@pytest.mark.asyncio
async def test_workspace_snapshot_reads_rows_on_the_sweep_thread() -> None:
    """The snapshot row read shares the sweep's worker thread and stays off the event loop."""
    loop_thread = threading.get_ident()
    seen: list[tuple[str, int]] = []
    machine = SimpleNamespace(id="machine-1", ref=None, hostname="local")
    workspace = SimpleNamespace(id="ws-1")

    def resolve_reference(_reference: str, node: str | None = None) -> SimpleNamespace:
        del node
        seen.append(("resolve", threading.get_ident()))
        return SimpleNamespace(tab=None, pane=None, workspace=workspace, node=machine)

    def sweep_dead_panes(_workspace_id: str) -> SimpleNamespace:
        seen.append(("sweep", threading.get_ident()))
        return SimpleNamespace(removed_panes=(), removed_tabs=())

    def get_workspace(_workspace_id: str) -> SimpleNamespace:
        seen.append(("workspace", threading.get_ident()))
        return workspace

    def list_tabs(_workspace_id: str) -> tuple[object, ...]:
        seen.append(("tabs", threading.get_ident()))
        return ()

    def list_panes(_workspace_id: str) -> tuple[object, ...]:
        seen.append(("panes", threading.get_ident()))
        return ()

    workspaces = MagicMock()
    workspaces.resolve_reference.side_effect = resolve_reference
    workspaces.sweep_dead_panes.side_effect = sweep_dead_panes
    workspaces.get.side_effect = get_workspace
    workspaces.list_tabs.side_effect = list_tabs
    workspaces.list_panes.side_effect = list_panes
    ops = _storage_ops(workspaces)
    with patch("gobby.terminals.workspace_contract.require_machine_id", return_value="machine-1"):
        snapshot = await ops.workspace_snapshot("operator", "ws-1")
    assert snapshot.workspace.id == "ws-1"
    sweep_threads = [thread for kind, thread in seen if kind == "sweep"]
    row_threads = [thread for kind, thread in seen if kind in {"workspace", "tabs", "panes"}]
    assert sweep_threads
    assert row_threads
    assert sweep_threads[0] != loop_thread
    assert all(thread == sweep_threads[0] for thread in row_threads)


@pytest.mark.asyncio
async def test_workspace_snapshot_log_separates_worker_wait_and_execution(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gobby.telemetry.query_timing import observe_queries, record_pool_acquire, record_query

    machine = SimpleNamespace(id="machine-1", ref=None, hostname="local")
    workspace = SimpleNamespace(id="ws-1")
    workspaces = MagicMock()

    def resolve_reference(_reference: str, *, node: str | None = None) -> SimpleNamespace:
        record_pool_acquire(0.2)
        record_query(0.05)
        return SimpleNamespace(tab=None, pane=None, workspace=workspace, node=machine)

    workspaces.resolve_reference.side_effect = resolve_reference
    workspaces.sweep_dead_panes.return_value = SimpleNamespace(removed_panes=(), removed_tabs=())
    workspaces.get.return_value = workspace
    workspaces.list_tabs.return_value = ()
    workspaces.list_panes.return_value = ()
    ops = _storage_ops(workspaces)

    caller_queries: list[float] = []
    caller_acquires: list[float] = []
    ticks = iter((0.0, 0.1, 0.2, 1.2, 1.25, 1.5, 1.55, 1.6, 1.65, 1.7, 2.0, 2.1))
    with (
        patch("gobby.terminals.workspace_ops.time", SimpleNamespace(monotonic=ticks.__next__)),
        patch("gobby.terminals.workspace_contract.require_machine_id", return_value="machine-1"),
        patch.object(ops, "_publish_removal", return_value=None),
        observe_queries(caller_queries.append, pool_acquire_observer=caller_acquires.append),
    ):
        snapshot = await ops.workspace_snapshot("operator", "ws-1")
        record_query(0.25)
        record_pool_acquire(0.3)

    assert snapshot.workspace.id == "ws-1"
    workspaces.sweep_dead_panes.assert_called_once_with("ws-1")
    workspaces.list_tabs.assert_called_once_with("ws-1")
    workspaces.list_panes.assert_called_once_with("ws-1")
    assert "worker_wait_ms=1000.0 worker_ms=500.0 worker_return_ms=300.0" in caplog.text
    assert "query_ms=50.0 query_count=1 pool_wait_ms=200.0 pool_acquires=1" in caplog.text
    assert caller_queries == [0.25]
    assert caller_acquires == [0.3]


@pytest.mark.asyncio
async def test_snapshot_watermark_excludes_a_publish_waiting_on_the_fence() -> None:
    """A workspace publish that arrives during the sweep stays above the snapshot seq."""
    seq = 0
    started = asyncio.Event()
    release = asyncio.Event()
    foreign: list[int] = []

    async def publish(event: object) -> int:
        nonlocal seq
        seq += 1
        current = seq
        kind = event["kind"] if isinstance(event, dict) else None
        if kind == "pane.removed":
            started.set()
            await release.wait()
        return current

    async def intruder() -> None:
        await started.wait()
        emitted = await ops._emit("workspace.created", "ws-1")
        assert emitted is not None
        foreign.append(emitted)

    pane = MagicMock()
    pane.to_dict.return_value = {"id": "pane-1"}
    machine = SimpleNamespace(id="machine-1", ref=None, hostname="local")
    workspace = SimpleNamespace(id="ws-1")

    def resolve_reference(_reference: str, node: str | None = None) -> SimpleNamespace:
        del node
        return SimpleNamespace(tab=None, pane=None, workspace=workspace, node=machine)

    def sweep_dead_panes(_workspace_id: str) -> SimpleNamespace:
        return SimpleNamespace(removed_panes=(pane,), removed_tabs=(), tabs=())

    workspaces = MagicMock()
    workspaces.resolve_reference.side_effect = resolve_reference
    workspaces.sweep_dead_panes.side_effect = sweep_dead_panes
    workspaces.get.return_value = workspace
    workspaces.list_tabs.return_value = ()
    workspaces.list_panes.return_value = ()
    ops = _storage_ops(workspaces)
    object.__setattr__(ops, "_publish", publish)
    intruder_task = asyncio.create_task(intruder())
    with patch("gobby.terminals.workspace_contract.require_machine_id", return_value="machine-1"):
        snapshot_task = asyncio.create_task(ops.workspace_snapshot("operator", "ws-1"))
        await asyncio.wait_for(started.wait(), timeout=2)
        yielded = asyncio.Event()

        async def _yield_once() -> None:
            yielded.set()

        asyncio.create_task(_yield_once())
        await yielded.wait()
        assert seq == 1
        release.set()
        snapshot = await snapshot_task
    await intruder_task
    assert snapshot.lifecycle_seq is not None
    assert foreign
    assert snapshot.lifecycle_seq < foreign[0]


@pytest.mark.asyncio
async def test_tab_rename_writes_storage_off_the_event_loop() -> None:
    """Renaming a tab must not write the row on the event-loop thread."""
    loop_thread = threading.get_ident()
    seen: list[int] = []
    tab = SimpleNamespace(id="tab-1", workspace_id="ws-1")
    target = SimpleNamespace(tab=tab, pane=None, workspace=SimpleNamespace(id="ws-1"))

    def rename_tab(_tab_id: str, title: str | None) -> SimpleNamespace:
        seen.append(threading.get_ident())
        return SimpleNamespace(id="tab-1", workspace_id="ws-1", title=title)

    workspaces = MagicMock()
    workspaces.rename_tab.side_effect = rename_tab
    ops = _storage_ops(workspaces)

    async def enter(_reference: str, _node: str | None) -> SimpleNamespace:
        return target

    async def emit(*_args: object, **_kwargs: object) -> None:
        return None

    object.__setattr__(ops, "_enter", enter)
    object.__setattr__(ops, "_emit", emit)
    renamed = await ops.tab_rename("operator", "tab-1", "renamed")
    assert renamed.id == "tab-1"
    assert seen
    assert seen[0] != loop_thread


@pytest.mark.asyncio
async def test_pane_wait_for_output_caps_a_huge_timeout(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client timeout of 1e9 must not schedule a poll past the 300s cap."""
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR)
    pane = (await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)).panes[0]
    h.native.snapshot_text = "still waiting"
    clock = {"now": 0.0}

    def monotonic() -> float:
        return clock["now"]

    async def advance(seconds: float) -> None:
        clock["now"] += seconds
        if clock["now"] > 300:
            raise AssertionError(f"wait reached {clock['now']} past the cap")

    monkeypatch.setattr("gobby.terminals.workspace_pane_io.time.monotonic", monotonic)
    monkeypatch.setattr("gobby.terminals.workspace_pane_io.asyncio.sleep", advance)
    waited = await h.ops.pane_wait_for_output(
        OPERATOR,
        pane.id,
        "NEEDLE-NOT-PRESENT",
        timeout_seconds=1e9,
        poll_interval_seconds=2.0,
    )
    assert waited.matched is False
    assert waited.reason == "timeout"


async def test_ops_refuse_in_flight_and_retry_orphaned_kill(harness: _Harness) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR)
    created = await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)
    tab, root = created.tabs[0], created.panes[0]
    other = (await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)).tabs[0]
    hot = str(uuid.uuid4())
    h.workspaces.mark_spawn_in_flight(hot)
    h.workspaces.add_pane(hot, beside=root.id, axis="vertical")
    rows = (
        tuple(h.workspaces.list_tabs(workspace.id)),
        tuple(h.workspaces.list_panes(workspace.id)),
    )
    kills = list(h.native.terminated_host_ids)

    refused: list[Callable[[], Awaitable[object]]] = [
        lambda: h.ops.workspace_close(OPERATOR, workspace.id),
        lambda: h.ops.tab_close(OPERATOR, tab.id),
        lambda: h.ops.tab_move(OPERATOR, tab.id, 1),
        lambda: h.ops.pane_swap(OPERATOR, root.id, hot),
        lambda: h.ops.pane_move(OPERATOR, hot, other.id),
        lambda: h.ops.pane_close(OPERATOR, hot),
    ]
    for operation in refused:
        await _raises("busy", operation())
    assert (
        tuple(h.workspaces.list_tabs(workspace.id)),
        tuple(h.workspaces.list_panes(workspace.id)),
    ) == rows
    assert list(h.native.terminated_host_ids) == kills

    # An orphaned seat keeps its pane, so closing it retries the kill.
    h.workspaces.clear_spawn_in_flight(hot)
    h.workspaces.remove_pane(hot)
    assert h.terminals.mark_orphaned(str(root.terminal_id)) is not None
    await h.ops.pane_close(OPERATOR, root.id)
    killed = h.terminals.get(str(root.terminal_id))
    assert killed is not None and killed.state == "exited"
    assert ("ht-1", killed.id) in h.native.terminated_host_ids
    assert root.id not in {pane.id for pane in h.workspaces.list_panes(workspace.id)}


@pytest.mark.parametrize("closing", ["workspace", "tab"])
async def test_close_refuses_membership_drift_since_read(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, closing: str
) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR)
    created = await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)
    tab, root = created.tabs[0], created.panes[0]
    settled = _live_terminal(h.terminals, h.project_id, "native")
    read = h.workspaces.list_panes
    reserved: list[str] = []

    def reserve_after_read(workspace_id: str) -> list[WorkspacePane]:
        panes = read(workspace_id)
        if not reserved:
            # A reservation inserts, binds and settles its pane before the close runs.
            agent = str(uuid.uuid4())
            h.workspaces.mark_spawn_in_flight(agent)
            h.workspaces.add_pane(agent, beside=root.id, axis="vertical")
            h.workspaces.set_pane_terminal(agent, settled.id, owns_terminal=True)
            h.workspaces.clear_spawn_in_flight(agent)
            reserved.append(agent)
        return panes

    monkeypatch.setattr(h.workspaces, "list_panes", reserve_after_read)
    kills = list(h.native.terminated_host_ids)
    if closing == "workspace":
        await _raises("busy", h.ops.workspace_close(OPERATOR, workspace.id))
    else:
        await _raises("busy", h.ops.tab_close(OPERATOR, tab.id))
    monkeypatch.undo()

    assert {pane.id for pane in h.workspaces.list_panes(workspace.id)} == {root.id, *reserved}
    for terminal_id in (str(root.terminal_id), settled.id):
        kept = h.terminals.get(terminal_id)
        assert kept is not None and kept.state == "live"
    assert list(h.native.terminated_host_ids) == kills


async def test_select_emits_focus_requested_where_hints_stay_passive(harness: _Harness) -> None:
    h = harness
    workspace = await h.ops.workspace_create(OPERATOR)
    spawned = await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)
    tab, pane = spawned.tabs[0], spawned.panes[0]

    # A window persisting its own focus stays a passive hint.
    await h.ops.workspace_set_focus_hints(
        OPERATOR, workspace.id, project_id=h.project_id, tab=tab.id, pane=pane.id
    )
    assert h.events[-1]["kind"] == "focus_hints"

    # An explicit select asks live windows to show the tab and pane, and stores
    # the tab's own project as the hint.
    selected, focused = await h.ops.workspace_select(OPERATOR, workspace.id, tab.id, pane=pane.id)
    requested = h.events[-1]
    assert requested["kind"] == "focus_requested"
    assert [(row["id"], row["project_id"]) for row in requested["tabs"]] == [(tab.id, h.project_id)]
    assert (selected.focused_project_id, selected.focused_tab_id) == (h.project_id, tab.id)
    assert focused is not None and focused.focused_pane_id == pane.id

    # The tab form keeps the tab's own pane focus.
    await h.ops.workspace_select(OPERATOR, workspace.id, tab.id)
    reselected = h.events[-1]
    assert reselected is not requested and reselected["kind"] == "focus_requested"
    assert [row.focused_pane_id for row in h.workspaces.list_tabs(workspace.id)] == [pane.id]
