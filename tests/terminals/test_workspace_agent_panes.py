"""Agent pane reservation primitives (plan placed-agent-launch 1.1)."""

from __future__ import annotations

import asyncio
import gc
import itertools
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import patch

import pytest

import gobby.terminals.workspace_agent_panes as agent_panes
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TITLE_MAX_BYTES, Terminal, TerminalManager, tmux_locator_key
from gobby.storage.workspaces import (
    Workspace,
    WorkspaceManager,
    WorkspacePane,
    WorkspaceTab,
    mint_pane_id,
)
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.termination import kill_terminal
from gobby.terminals.workspace_agent_panes import (
    AgentPaneReserver,
    AgentPlacement,
    AgentPlacementError,
    ReservedPane,
)
from gobby.terminals.workspace_contract import WorkspaceEvent, WorkspaceOpError
from gobby.terminals.workspace_ops import WorkspaceOps
from gobby.terminals.write_coordinator import WriteCoordinator
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID
from tests.terminals.fakes import FakeRuntime, runtime_registry

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000001"
REMOTE_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000002"
OPERATOR = "operator"
MESSAGE_MARKER = "exception-message-marker"
_TMUX_PANES = itertools.count(1)


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@dataclass
class _Harness:
    db: HubDatabase
    workspaces: WorkspaceManager
    terminals: TerminalManager
    sessions: SessionManager
    tmux: FakeRuntime
    events: list[WorkspaceEvent]
    publish_failures: dict[str, Exception]
    minted: list[str]
    project_id: str
    reserver: AgentPaneReserver
    ops: WorkspaceOps


@pytest.fixture
def harness(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> _Harness:
    LocalMachineManager(temp_db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID, hostname="local")
    workspaces, terminals = WorkspaceManager(temp_db), TerminalManager(temp_db)
    sessions = SessionManager(temp_db)
    tmux = FakeRuntime(backend="tmux")
    registry = runtime_registry(tmux, FakeRuntime(backend="native"))
    events: list[WorkspaceEvent] = []
    publish_failures: dict[str, Exception] = {}

    async def publish(event: WorkspaceEvent) -> None:
        failure = publish_failures.pop(event["kind"], None)
        if failure is not None:
            raise failure
        events.append(event)

    minted: list[str] = []

    def recording_mint() -> str:
        minted.append(mint_pane_id())
        return minted[-1]

    monkeypatch.setattr(agent_panes, "mint_pane_id", recording_mint)
    return _Harness(
        db=temp_db,
        workspaces=workspaces,
        terminals=terminals,
        sessions=sessions,
        tmux=tmux,
        events=events,
        publish_failures=publish_failures,
        minted=minted,
        project_id=str(sample_project["id"]),
        reserver=AgentPaneReserver(
            workspaces=workspaces,
            terminals=terminals,
            registry=registry,
            sessions=sessions,
            publish=publish,
        ),
        ops=WorkspaceOps(
            workspaces=workspaces,
            terminals=terminals,
            registry=registry,
            coordinator=WriteCoordinator(
                terminals,
                registry,
                lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
            ),
            sessions=sessions,
            publish=publish,
        ),
    )


def _workspace(h: _Harness, name: str = "agents") -> Workspace:
    return h.workspaces.create(LOCAL_MACHINE_ID, name)[0]


def _terminal(h: _Harness, state: str = "live") -> Terminal:
    """A tmux terminal row in ``state``: pending, live, orphaned or exited."""
    terminal_id = str(uuid.uuid4())
    row = h.terminals.create_pending(
        terminal_id, h.project_id, "tmux", "gobby", terminal_id, machine_id=LOCAL_MACHINE_ID
    )
    if state == "pending":
        return row
    pane_id = f"%{next(_TMUX_PANES)}"
    socket = "/private/tmp/tmux-501/agent-panes"
    live = h.terminals.promote_to_live(
        row.id,
        locator={
            "socket_path": socket,
            "server_pid": 1658,
            "server_start_time": 1784592177,
            "pane_id": pane_id,
        },
        locator_key=tmux_locator_key(
            socket_path=socket, server_pid=1658, server_start_time=1784592177, pane_id=pane_id
        ),
        session_name=row.spawn_key,
    )
    assert live is not None
    if state == "live":
        return live
    settled = (
        h.terminals.mark_orphaned(live.id)
        if state == "orphaned"
        else h.terminals.mark_exited(live.id)
    )
    assert settled is not None
    return settled


def _state(h: _Harness, terminal: Terminal) -> str:
    row = h.terminals.get(terminal.id)
    assert row is not None
    return row.state


def _base(
    h: _Harness,
    workspace: Workspace,
    *,
    title: str | None = None,
    terminal: Terminal | None = None,
    project_id: str | None = None,
) -> tuple[WorkspaceTab, WorkspacePane]:
    """An ordinary tab whose one pane holds ``terminal`` (a fresh live one by default)."""
    change = h.workspaces.create_tab(
        workspace.id,
        pane_id=str(uuid.uuid4()),
        project_id=project_id or h.project_id,
        title=title,
    )
    held = terminal or _terminal(h)
    pane = h.workspaces.set_pane_terminal(change.panes[0].id, held.id, owns_terminal=True)
    assert pane is not None
    return change.tabs[0], pane


def _labeled_split(
    h: _Harness, beside: WorkspacePane, label: str, terminal: Terminal
) -> WorkspacePane:
    pane_id = str(uuid.uuid4())
    h.workspaces.add_pane(pane_id, beside=beside.id, axis="horizontal")
    h.workspaces.rename_pane(pane_id, label)
    pane = h.workspaces.set_pane_terminal(pane_id, terminal.id, owns_terminal=True)
    assert pane is not None
    return pane


def _tab(workspace_ref: str, title: str) -> AgentPlacement:
    return AgentPlacement.parse({"tab": {"workspace": workspace_ref, "title": title}})


def _split(pane_ref: str, title: str, axis: str = "right") -> AgentPlacement:
    return AgentPlacement.parse({"split": {"pane": pane_ref, "axis": axis, "title": title}})


async def _reserve(
    h: _Harness, placement: AgentPlacement, *, worktree_id: str | None = None
) -> ReservedPane:
    resolved = await h.reserver.preflight(OPERATOR, h.project_id, placement)
    return await h.reserver.reserve(resolved, worktree_id=worktree_id)


async def _launch(h: _Harness, placement: AgentPlacement) -> tuple[ReservedPane, Terminal]:
    terminal = _terminal(h)
    reserved = await _reserve(h, placement)
    await h.reserver.bind(reserved, terminal.id)
    return reserved, terminal


async def _refused(code: str, operation: Awaitable[object]) -> AgentPlacementError:
    with pytest.raises(AgentPlacementError) as caught:
        await operation
    assert caught.value.code == code, str(caught.value)
    return caught.value


async def _op_refused(code: str, operation: Awaitable[object]) -> None:
    with pytest.raises(WorkspaceOpError) as caught:
        await operation
    assert caught.value.code == code, str(caught.value)


def _raiser(exc: Exception) -> Callable[..., NoReturn]:
    def fail(*args: object, **kwargs: object) -> NoReturn:
        raise exc

    return fail


def _pane_ids(h: _Harness, *workspaces: Workspace) -> set[str]:
    return {pane.id for ws in workspaces for pane in h.workspaces.list_panes(ws.id)}


def _tab_ids(h: _Harness, workspace: Workspace) -> set[str]:
    return {tab.id for tab in h.workspaces.list_tabs(workspace.id)}


def _pane(h: _Harness, workspace: Workspace, pane_id: str) -> WorkspacePane:
    return next(pane for pane in h.workspaces.list_panes(workspace.id) if pane.id == pane_id)


def _tab_row(h: _Harness, workspace: Workspace, tab_id: str) -> WorkspaceTab:
    return next(tab for tab in h.workspaces.list_tabs(workspace.id) if tab.id == tab_id)


def _in_flight(h: _Harness) -> list[str]:
    return [pane_id for pane_id in h.minted if h.workspaces.is_spawn_in_flight(pane_id)]


def _reserver_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Records the reserver emitted; its loop paths must log nothing (Josh's loop-logging ban)."""
    return [record.getMessage() for record in caplog.records if record.name == agent_panes.__name__]


def _kinds(h: _Harness) -> list[str]:
    return [event["kind"] for event in h.events]


def _split_axis(node: Mapping[str, object], pane_id: str) -> object:
    """The axis of the split whose direct child is ``pane_id``'s leaf."""
    children = node.get("children")
    if not isinstance(children, list):
        return None
    for child in children:
        assert isinstance(child, Mapping)
        if child.get("pane_id") == pane_id:
            return node["axis"]
        found = _split_axis(child, pane_id)
        if found is not None:
            return found
    return None


async def test_invalid_placement_shapes_refused(harness: _Harness) -> None:
    h = harness
    ws = _workspace(h)
    pane_id = str(uuid.uuid4())
    tab_body = {"workspace": ws.id, "title": "seat"}
    split_body = {"pane": pane_id, "axis": "right", "title": "seat"}
    invalid: list[object] = [
        None,
        "tab",
        {},
        {"window": tab_body},
        {"tab": {"workspace": ws.id}},
        {"tab": {"workspace": ws.id, "title": ""}},
        {"tab": {"workspace": ws.id, "title": "   "}},
        {"tab": {"workspace": ws.id, "title": 7}},
        {"tab": {"workspace": "", "title": "seat"}},
        {"split": {"pane": pane_id, "title": "seat"}},
        {"split": {**split_body, "axis": "left"}},
        {"split": {**split_body, "axis": "horizontal"}},
        {"tab": tab_body, "split": split_body},
        {"tab": {**tab_body, "position": 0}},
        {"split": {**split_body, "worktree": "w"}},
    ]
    for raw in invalid:
        with pytest.raises(AgentPlacementError) as caught:
            AgentPlacement.parse(raw)
        assert caught.value.code == "invalid_placement", raw

    assert AgentPlacement.parse({"tab": tab_body}) == AgentPlacement(
        kind="tab", ref=ws.id, title="seat"
    )
    assert h.workspaces.list_tabs(ws.id) == []
    assert h.events == [] and h.minted == []


async def test_preflight_refusals_have_no_side_effects(harness: _Harness) -> None:
    h = harness
    ws = _workspace(h)
    _, beside = _base(h, ws)
    other = LocalProjectManager(h.db).create(name="agent-panes-outsider")
    outsider = h.sessions.register(
        external_id="agent-panes-outsider",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=other.id,
    )
    remote = LocalMachineManager(h.db).upsert_seen(
        REMOTE_MACHINE_ID, TEST_USER_ID, hostname="remote-node"
    )
    remote_ws = h.workspaces.create(remote.id, "remote")[0]
    panes, tabs = _pane_ids(h, ws, remote_ws), _tab_ids(h, ws)

    cases: list[tuple[str, str, str, AgentPlacement]] = [
        ("not_found", OPERATOR, h.project_id, _tab(str(uuid.uuid4()), "seat")),
        ("not_found", OPERATOR, h.project_id, _tab("no-such-workspace", "seat")),
        ("not_found", OPERATOR, h.project_id, _split(str(uuid.uuid4()), "seat")),
        ("invalid_ref", OPERATOR, h.project_id, _tab(beside.id, "seat")),
        ("invalid_ref", OPERATOR, h.project_id, _split(ws.id, "seat")),
        ("forbidden", "nobody", h.project_id, _tab(ws.id, "seat")),
        ("forbidden", f"session:{outsider.id}", h.project_id, _tab(ws.id, "seat")),
        ("forbidden", f"session:{outsider.id}", h.project_id, _split(beside.id, "seat")),
        ("forbidden", OPERATOR, other.id, _split(beside.id, "seat")),
        ("invalid_op", OPERATOR, h.project_id, _tab(remote_ws.id, "seat")),
    ]
    for code, actor, project_id, placement in cases:
        await _refused(code, h.reserver.preflight(actor, project_id, placement))

    assert _pane_ids(h, ws, remote_ws) == panes
    assert _tab_ids(h, ws) == tabs and _tab_ids(h, remote_ws) == set()
    assert h.events == [] and h.minted == []


async def test_live_seat_refused_across_kinds_ended_seat_allowed(harness: _Harness) -> None:
    h = harness
    for state in ("pending", "live", "orphaned"):
        ws = _workspace(h, f"seats-{state}")
        _, anchor = _base(h, ws)
        _base(h, ws, title="alpha", terminal=_terminal(h, state))
        _labeled_split(h, anchor, "beta", _terminal(h, state))
        # A tab seat refuses a split request and a split seat refuses a tab request.
        for title in ("alpha", "beta"):
            for placement in (_tab(ws.id, title), _split(anchor.id, title)):
                await _refused("seat_live", h.reserver.preflight(OPERATOR, h.project_id, placement))

    # Two titles that truncate to one stored value are one seat.
    ws = _workspace(h, "truncation")
    prefix = "x" * TITLE_MAX_BYTES
    held = _terminal(h)
    _base(h, ws, title=f"{prefix}a", terminal=held)
    await _refused(
        "seat_live", h.reserver.preflight(OPERATOR, h.project_id, _tab(ws.id, f"{prefix}b"))
    )

    # Once that terminal has ended, the seat relaunches.
    assert h.terminals.mark_exited(held.id) is not None
    resolved = await h.reserver.preflight(OPERATOR, h.project_id, _tab(ws.id, f"{prefix}b"))
    assert resolved.seat == prefix
    assert h.events == [] and h.minted == []


async def test_reserve_bind_emits_once(harness: _Harness) -> None:
    h = harness
    ws = _workspace(h)
    node = h.workspaces.resolve_node()
    assert node.ref is not None
    _, anchor = _base(h, ws)
    for placement, kind in (
        (_tab(ws.id, "lead"), "tab.created"),
        (_split(anchor.id, "scout"), "pane.added"),
    ):
        h.events.clear()
        reserved = await _reserve(h, placement)
        pane, tab = _pane(h, ws, reserved.pane_id), _tab_row(h, ws, reserved.tab_id)
        assert pane.terminal_id is None
        assert reserved.workspace_id == ws.id
        assert reserved.tab_ref == f"{node.ref}:{ws.ref}:{tab.ref}"
        assert reserved.pane_ref == f"{node.ref}:{ws.ref}:{tab.ref}:{pane.ref}"
        assert (tab.title if kind == "tab.created" else pane.label) == placement.title
        assert h.workspaces.is_spawn_in_flight(reserved.pane_id)
        assert h.events == []

        terminal = _terminal(h)
        bound = await h.reserver.bind(reserved, terminal.id)
        assert (bound.id, bound.terminal_id, bound.owns_terminal) == (
            reserved.pane_id,
            terminal.id,
            True,
        )
        assert _kinds(h) == [kind]
        assert [row["id"] for row in h.events[0]["panes"]] == [reserved.pane_id]
        assert [row["id"] for row in h.events[0]["tabs"]] == [reserved.tab_id]
        assert h.workspaces.is_spawn_in_flight(reserved.pane_id)

        h.reserver.settle(reserved)
        assert not h.workspaces.is_spawn_in_flight(reserved.pane_id)
        assert _kinds(h) == [kind]


async def test_release_is_idempotent(harness: _Harness) -> None:
    h = harness
    ws = _workspace(h)
    reserved = await _reserve(h, _tab(ws.id, "lead"))
    for _attempt in range(2):
        await h.reserver.release(reserved, terminal_id=None)
        assert h.workspaces.list_tabs(ws.id) == []
        assert _kinds(h) == ["pane.removed", "tab.removed"]
        assert _in_flight(h) == []

    # An inactive launch terminal is not killed; the split goes and its tab stays.
    anchor_tab, anchor = _base(h, ws)
    h.events.clear()
    reserved = await _reserve(h, _split(anchor.id, "scout"))
    ended = _terminal(h, "exited")
    for _attempt in range(2):
        await h.reserver.release(reserved, terminal_id=ended.id)
        assert _pane_ids(h, ws) == {anchor.id}
        assert _tab_ids(h, ws) == {anchor_tab.id}
        assert _kinds(h) == ["pane.removed"]
    assert h.tmux.killed_ids == set()
    assert _in_flight(h) == []
    await h.reserver.preflight(OPERATOR, h.project_id, _split(anchor.id, "scout"))


async def test_concurrent_same_seat_reserves_once(harness: _Harness) -> None:
    h = harness
    ws = _workspace(h)
    _, anchor = _base(h, ws)
    for index, kinds in enumerate(
        (("tab", "tab"), ("split", "split"), ("tab", "split"), ("split", "tab"))
    ):
        title = f"seat-{index}"
        resolved = [
            await h.reserver.preflight(
                OPERATOR,
                h.project_id,
                _tab(ws.id, title) if kind == "tab" else _split(anchor.id, title),
            )
            for kind in kinds
        ]
        outcomes = await asyncio.gather(
            *(h.reserver.reserve(placed, worktree_id=None) for placed in resolved),
            return_exceptions=True,
        )
        reserved = [outcome for outcome in outcomes if isinstance(outcome, ReservedPane)]
        refused = [outcome for outcome in outcomes if isinstance(outcome, AgentPlacementError)]
        assert len(reserved) == 1, outcomes
        assert [error.code for error in refused] == ["seat_live"], outcomes
        assert _in_flight(h) == [reserved[0].pane_id]
        await h.reserver.release(reserved[0], terminal_id=None)


async def test_split_axis_maps_to_storage_axis(harness: _Harness) -> None:
    h = harness
    ws = _workspace(h)
    _, anchor = _base(h, ws)
    for axis, stored in (("right", "horizontal"), ("down", "vertical")):
        placement = _split(anchor.id, f"seat-{axis}", axis)
        assert placement.axis == stored
        reserved = await _reserve(h, placement)
        layout = _tab_row(h, ws, reserved.tab_id).layout
        assert _split_axis(layout, reserved.pane_id) == stored


async def test_reserve_stores_final_worktree_association(harness: _Harness, tmp_path: Path) -> None:
    h = harness
    ws = _workspace(h)
    worktrees = LocalWorktreeManager(h.db)
    reused = worktrees.create(h.project_id, "agent-reused", str(tmp_path / "reused"))
    fresh = worktrees.create(h.project_id, "agent-fresh", str(tmp_path / "fresh"))
    tabs: dict[str | None, ReservedPane] = {}
    for title, worktree_id in (("none", None), ("reused", reused.id), ("fresh", fresh.id)):
        tabs[worktree_id] = await _reserve(h, _tab(ws.id, title), worktree_id=worktree_id)
        assert _tab_row(h, ws, tabs[worktree_id].tab_id).worktree_id == worktree_id

    # A split joins its tab and stores no worktree of its own.
    split = await _reserve(h, _split(tabs[reused.id].pane_id, "scout"), worktree_id=fresh.id)
    assert split.tab_id == tabs[reused.id].tab_id
    assert _tab_row(h, ws, split.tab_id).worktree_id == reused.id


async def test_reserve_insert_failure_leaves_nothing(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    ws = _workspace(h)
    anchor_tab, anchor = _base(h, ws)
    for method, placement in (
        ("create_tab", _tab(ws.id, "lead")),
        ("add_pane", _split(anchor.id, "scout")),
        ("rename_pane", _split(anchor.id, "scout")),
    ):
        resolved = await h.reserver.preflight(OPERATOR, h.project_id, placement)
        with monkeypatch.context() as patched:
            patched.setattr(h.workspaces, method, _raiser(RuntimeError(f"{method} failed")))
            with pytest.raises(RuntimeError, match=f"{method} failed"):
                await h.reserver.reserve(resolved, worktree_id=None)
        assert _tab_ids(h, ws) == {anchor_tab.id}
        assert _pane_ids(h, ws) == {anchor.id}
        assert _tab_row(h, ws, anchor_tab.id).layout == anchor_tab.layout
        assert _in_flight(h) == []

        retry = await _reserve(h, placement)
        await h.reserver.release(retry, terminal_id=None)


async def test_reserve_cancelled_before_return_leaves_nothing(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    h = harness
    ws = _workspace(h)
    anchor_tab, anchor = _base(h, ws)
    caplog.set_level(logging.DEBUG, logger=agent_panes.__name__)
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    # (method held open, placement, cancellations, insert raises after it commits)
    cases: list[tuple[str, AgentPlacement, int, bool]] = [
        ("create_tab", _tab(ws.id, "lead"), 1, False),
        ("rename_pane", _split(anchor.id, "scout"), 2, False),
        ("create_tab", _tab(ws.id, "lead"), 2, True),
    ]
    try:
        for method, placement, cancellations, insert_fails in cases:
            committed, gate = threading.Event(), threading.Event()
            original = getattr(h.workspaces, method)

            def held(
                *args: Any,
                _original: Callable[..., Any] = original,
                _committed: threading.Event = committed,
                _gate: threading.Event = gate,
                _fails: bool = insert_fails,
                **kwargs: Any,
            ) -> Any:
                result = _original(*args, **kwargs)
                _committed.set()
                assert _gate.wait(5)
                if _fails:
                    raise RuntimeError(MESSAGE_MARKER)
                return result

            resolved = await h.reserver.preflight(OPERATOR, h.project_id, placement)
            with monkeypatch.context() as patched:
                patched.setattr(h.workspaces, method, held)
                task = asyncio.create_task(h.reserver.reserve(resolved, worktree_id=None))
                assert await asyncio.to_thread(committed.wait, 5)
                for _cancel in range(cancellations):
                    task.cancel()
                    for _tick in range(5):
                        await asyncio.sleep(0)
                    # Every cancellation waits for the running insert and its rollback.
                    assert not task.done()
                gate.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert _tab_ids(h, ws) == {anchor_tab.id}
            assert _pane_ids(h, ws) == {anchor.id}
            assert _in_flight(h) == []

            retry = await _reserve(h, placement)
            await h.reserver.release(retry, terminal_id=None)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)
    # A cancelled insert's failure is consumed: neither the loop nor the reserver reports it.
    assert unhandled == []
    assert _reserver_logs(caplog) == []


async def test_release_kills_only_an_active_owned_terminal(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    ws = _workspace(h)
    kills: list[str] = []
    failing: set[str] = set()

    async def recording_kill(*args: Any, **kwargs: Any) -> Terminal | None:
        terminal: Terminal = args[2]
        kills.append(terminal.id)
        if terminal.id in failing:
            raise RuntimeError("kill failed")
        return await kill_terminal(*args, **kwargs)

    monkeypatch.setattr(agent_panes, "kill_terminal", recording_kill)

    # An owned terminal that is still pending or live is killed, then its pane goes.
    for state in ("pending", "live"):
        terminal = _terminal(h, state)
        reserved = await _reserve(h, _tab(ws.id, f"active-{state}"))
        await h.reserver.bind(reserved, terminal.id)
        await h.reserver.release(reserved, terminal_id=terminal.id)
        assert kills[-1] == terminal.id
        assert reserved.pane_id not in _pane_ids(h, ws)
    assert _state(h, terminal) == "exited"

    # An inactive terminal is not killed.
    ended = _terminal(h, "exited")
    reserved = await _reserve(h, _tab(ws.id, "inactive"))
    await h.reserver.bind(reserved, ended.id)
    attempts = len(kills)
    await h.reserver.release(reserved, terminal_id=ended.id)
    assert len(kills) == attempts
    assert reserved.pane_id not in _pane_ids(h, ws)

    # A failed kill keeps the pane bound to the launch terminal, which holds the seat:
    # a live row is orphaned and a pending row stays pending.
    for state, settled in (("live", "orphaned"), ("pending", "pending")):
        terminal = _terminal(h, state)
        failing.add(terminal.id)
        title = f"failed-{state}"
        reserved = await _reserve(h, _tab(ws.id, title))
        if state == "live":
            await h.reserver.bind(reserved, terminal.id)
        await h.reserver.release(reserved, terminal_id=terminal.id)
        assert kills[-1] == terminal.id
        assert _state(h, terminal) == settled
        pane = _pane(h, ws, reserved.pane_id)
        assert (pane.terminal_id, pane.owns_terminal) == (terminal.id, True)
        assert _in_flight(h) == []
        await _refused(
            "seat_live", h.reserver.preflight(OPERATOR, h.project_id, _tab(ws.id, title))
        )

    # An orphaned terminal is not killed; its pane holds the seat until the terminal exits.
    orphan = _terminal(h)
    reserved = await _reserve(h, _tab(ws.id, "orphaned"))
    await h.reserver.bind(reserved, orphan.id)
    assert h.terminals.mark_orphaned(orphan.id) is not None
    attempts = len(kills)
    await h.reserver.release(reserved, terminal_id=orphan.id)
    assert len(kills) == attempts
    assert _pane(h, ws, reserved.pane_id).terminal_id == orphan.id
    await _refused(
        "seat_live", h.reserver.preflight(OPERATOR, h.project_id, _tab(ws.id, "orphaned"))
    )
    assert h.terminals.mark_exited(orphan.id) is not None
    swept = h.workspaces.sweep_dead_panes(ws.id)
    assert reserved.pane_id in {pane.id for pane in swept.removed_panes}
    await h.reserver.preflight(OPERATOR, h.project_id, _tab(ws.id, "orphaned"))


async def test_release_steps_are_independent(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    h = harness
    ws = _workspace(h)
    caplog.set_level(logging.DEBUG, logger=agent_panes.__name__)
    kills: list[str] = []
    kill_failures: list[Exception] = []

    async def recording_kill(*args: Any, **kwargs: Any) -> Terminal | None:
        terminal: Terminal = args[2]
        kills.append(terminal.id)
        if kill_failures:
            raise kill_failures.pop()
        return await kill_terminal(*args, **kwargs)

    monkeypatch.setattr(agent_panes, "kill_terminal", recording_kill)

    # The kill raises: the pane stays with its orphaned terminal, and release returns.
    killed_fails, failed_terminal = await _launch(h, _tab(ws.id, "kill-raises"))
    kill_failures.append(RuntimeError(MESSAGE_MARKER))
    await h.reserver.release(killed_fails, terminal_id=failed_terminal.id)
    assert _state(h, failed_terminal) == "orphaned"

    # remove_pane raises after the kill: the residue is out of flight and swept.
    reserved, terminal = await _launch(h, _tab(ws.id, "remove-raises"))
    with monkeypatch.context() as patched:
        patched.setattr(h.workspaces, "remove_pane", _raiser(RuntimeError(MESSAGE_MARKER)))
        await h.reserver.release(reserved, terminal_id=terminal.id)
    assert kills[-1] == terminal.id and _state(h, terminal) == "exited"
    assert _in_flight(h) == []
    swept = h.workspaces.sweep_dead_panes(ws.id)
    assert reserved.pane_id in {pane.id for pane in swept.removed_panes}

    # The removal publish raises after the row is gone.
    reserved, terminal = await _launch(h, _tab(ws.id, "publish-raises"))
    h.publish_failures["pane.removed"] = RuntimeError(MESSAGE_MARKER)
    await h.reserver.release(reserved, terminal_id=terminal.id)
    assert kills[-1] == terminal.id and _state(h, terminal) == "exited"
    assert reserved.pane_id not in _pane_ids(h, ws)

    # The row is already gone.
    reserved, terminal = await _launch(h, _tab(ws.id, "row-gone"))
    h.workspaces.remove_pane(reserved.pane_id)
    await h.reserver.release(reserved, terminal_id=terminal.id)
    assert kills[-1] == terminal.id and _state(h, terminal) == "exited"

    # Cleanup already marked the terminal inactive, so release issues no kill.
    reserved, terminal = await _launch(h, _tab(ws.id, "settled"))
    assert h.terminals.mark_exited(terminal.id) is not None
    attempts = len(kills)
    await h.reserver.release(reserved, terminal_id=terminal.id)
    assert len(kills) == attempts
    assert reserved.pane_id not in _pane_ids(h, ws)

    # Every release cleared its mark and seat entry: each seat reserves again.
    assert _in_flight(h) == []
    assert h.terminals.mark_exited(failed_terminal.id) is not None
    h.workspaces.sweep_dead_panes(ws.id)
    for title in ("kill-raises", "remove-raises", "publish-raises", "row-gone", "settled"):
        await _reserve(h, _tab(ws.id, title))
    # Failed steps surface through the kept or swept pane above, never through logs.
    assert _reserver_logs(caplog) == []


async def test_reserve_rollback_failure_leaves_sweepable_residue(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    h = harness
    ws = _workspace(h)
    _, anchor = _base(h, ws)
    caplog.set_level(logging.DEBUG, logger=agent_panes.__name__)
    with monkeypatch.context() as patched:
        patched.setattr(h.workspaces, "rename_pane", _raiser(RuntimeError("label failed")))
        patched.setattr(h.workspaces, "remove_pane", _raiser(RuntimeError(MESSAGE_MARKER)))
        with pytest.raises(RuntimeError, match="label failed"):
            await _reserve(h, _split(anchor.id, "scout"))

    [residue] = h.minted
    assert _reserver_logs(caplog) == []
    assert _in_flight(h) == []
    assert _pane(h, ws, residue).terminal_id is None

    retry = await _reserve(h, _split(anchor.id, "scout"))
    swept = h.workspaces.sweep_dead_panes(ws.id)
    assert {pane.id for pane in swept.removed_panes} == {residue}
    assert _pane_ids(h, ws) == {anchor.id, retry.pane_id}


async def test_mark_held_until_settle(harness: _Harness) -> None:
    h = harness
    ws = _workspace(h)
    _, anchor = _base(h, ws)
    other_tab, _ = _base(h, ws)
    tab_seat, _ = await _launch(h, _tab(ws.id, "lead"))
    split_seat, _ = await _launch(h, _split(anchor.id, "scout"))

    guarded: list[Callable[[], Awaitable[object]]] = [
        partial(h.ops.pane_close, OPERATOR, tab_seat.pane_id),
        partial(h.ops.pane_close, OPERATOR, split_seat.pane_id),
        partial(h.ops.pane_swap, OPERATOR, anchor.id, split_seat.pane_id),
        partial(h.ops.pane_move, OPERATOR, split_seat.pane_id, other_tab.id),
    ]
    for operation in guarded:
        await _op_refused("busy", operation())
    assert {tab_seat.pane_id, split_seat.pane_id} <= _pane_ids(h, ws)

    h.reserver.settle(tab_seat)
    h.reserver.settle(split_seat)
    assert _in_flight(h) == []
    await h.ops.pane_swap(OPERATOR, anchor.id, split_seat.pane_id)
    await h.ops.pane_close(OPERATOR, tab_seat.pane_id)
    assert tab_seat.pane_id not in _pane_ids(h, ws)


async def test_split_reserve_refuses_moved_target(harness: _Harness) -> None:
    h = harness
    ws, elsewhere = _workspace(h), _workspace(h, "elsewhere")
    other = LocalProjectManager(h.db).create(name="agent-panes-moved")
    _, stay = _base(h, ws)
    destination, _ = _base(h, ws)
    foreign, _ = _base(h, ws, project_id=other.id)

    def move_pane_to(tab: WorkspaceTab) -> Callable[[WorkspaceTab, WorkspacePane], object]:
        return lambda _home, beside: h.workspaces.move_pane(beside.id, tab_id=tab.id)

    moves: list[Callable[[WorkspaceTab, WorkspacePane], object]] = [
        move_pane_to(destination),
        move_pane_to(foreign),
        lambda home, _beside: h.workspaces.move_tab(home.id, workspace_id=elsewhere.id, position=0),
    ]
    for index, move in enumerate(moves):
        home, beside = _base(h, ws)
        _labeled_split(h, beside, f"neighbour-{index}", _terminal(h))
        resolved = await h.reserver.preflight(
            OPERATOR, h.project_id, _split(beside.id, f"seat-{index}")
        )
        move(home, beside)
        panes = _pane_ids(h, ws, elsewhere)
        await _refused("not_found", h.reserver.reserve(resolved, worktree_id=None))
        assert _pane_ids(h, ws, elsewhere) == panes
        assert _in_flight(h) == []
    assert stay.id in _pane_ids(h, ws)
    assert h.events == []
