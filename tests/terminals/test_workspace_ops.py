"""WorkspaceOps contract (plan gclient-workspaces 2.1)."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import Awaitable, Coroutine, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal
from unittest.mock import patch

import pytest

from gobby.agents.constants import (
    GOBBY_NODE_ID,
    GOBBY_NODE_REF,
    GOBBY_PANE_ID,
    GOBBY_PANE_REF,
    GOBBY_TAB_ID,
    GOBBY_WORKSPACE_ID,
)
from gobby.mcp_proxy.tools.sessions._terminal_send_keys import _authorize_send_keys_target
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.project_checkouts import require_root
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import (
    Terminal,
    TerminalManager,
    native_locator_key,
    tmux_locator_key,
)
from gobby.storage.workspaces import LayoutChange, WorkspaceManager, layout_pane_ids
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.runtime import PreparedSpawn, TerminalRuntimeRegistry, TerminalSpawnRequest
from gobby.terminals.workspace_ops import WorkspaceEvent, WorkspaceOpError, WorkspaceOps
from gobby.terminals.write_coordinator import WriteCoordinator
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID
from tests.terminals.fakes import FakeRuntime, runtime_registry

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000001"
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
    return f"n{node.ref}:w{workspace_ref}:t{tab_ref}:p{pane_ref}"


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
        GOBBY_NODE_REF: f"n{node.ref}",
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
    assert owned.ref == 3
    destination = await h.ops.tab_create(OPERATOR, workspace.id, h.project_id)
    destination_tab, destination_pane = destination.tabs[0], destination.panes[0]
    moved = await h.ops.pane_move(
        OPERATOR, owned.id, destination_tab.id, beside=destination_pane.id
    )
    tabs = {row.id: row for row in moved.tabs}
    assert layout_pane_ids(tabs[tab.id].layout) == [agent_pane.id, external_pane.id]
    assert layout_pane_ids(tabs[destination_tab.id].layout) == [destination_pane.id, owned.id]
    assert (moved.panes[0].tab_id, moved.panes[0].ref) == (destination_tab.id, 2)

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
        refused: list[Coroutine[Any, Any, object]] = [
            h.ops.pane_read(actor, pane.id),
            h.ops.pane_send_text(actor, pane.id, "ls", submit=True),
            h.ops.pane_send_keys(actor, pane.id, "enter", literal=False),
            h.ops.pane_wait_for_output(actor, pane.id, "x", timeout_seconds=0),
            h.ops.pane_split(actor, pane.id, "horizontal"),
            h.ops.tab_create(actor, workspace.id, h.project_id),
            h.ops.tab_create(actor, workspace.id, h.project_id, terminal_id=agent.id),
            h.ops.pane_close(actor, pane.id),
            h.ops.tab_close(actor, tab.id),
            h.ops.workspace_close(actor, workspace.id),
        ]
        for operation in refused:
            await _raises("forbidden", operation)
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
        await _raises("invalid_ref", h.ops.pane_rename(OPERATOR, "w1:t1:bogus", "x")),
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
