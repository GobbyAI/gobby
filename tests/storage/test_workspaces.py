"""WorkspaceManager storage contract (plan gclient-workspaces 1.2)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from threading import Event, Thread, current_thread
from typing import Any
from unittest.mock import patch

import psycopg
import pytest

from gobby.storage import workspaces as workspaces_module
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.terminals import Terminal, TerminalManager, native_locator_key
from gobby.storage.workspaces import (
    InvalidWorkspaceOpError,
    InvalidWorkspaceRefError,
    WorkspaceBusyError,
    WorkspaceManager,
    WorkspaceNotFoundError,
    WorkspaceTarget,
    layout_pane_ids,
    validate_layout,
)
from gobby.terminals.in_doubt import in_doubt_spawns
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def manager(temp_db: HubDatabase) -> WorkspaceManager:
    LocalMachineManager(temp_db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID, hostname="local")
    return WorkspaceManager(temp_db)


def _pane_id() -> str:
    return str(uuid.uuid4())


def _leaf(pane_id: str) -> dict[str, str]:
    return {"kind": "pane", "pane_id": pane_id}


def _target_ids(target: WorkspaceTarget) -> tuple[str, str | None, str | None]:
    tab_id = None if target.tab is None else target.tab.id
    pane_id = None if target.pane is None else target.pane.id
    return target.workspace.id, tab_id, pane_id


def _terminal(terminals: TerminalManager, project_id: str, *, live: bool) -> Terminal:
    terminal_id = str(uuid.uuid4())
    pending = terminals.create_pending(
        terminal_id=terminal_id,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=terminal_id,
        machine_id=LOCAL_MACHINE_ID,
    )
    if not live:
        return pending
    host_terminal_id = str(uuid.uuid4())
    promoted = terminals.promote_to_live(
        pending.id,
        locator={"host_terminal_id": host_terminal_id},
        locator_key=native_locator_key("epoch", host_terminal_id),
        host_epoch="epoch",
    )
    assert promoted is not None
    return promoted


def test_refs_are_lowest_free_and_reused(
    manager: WorkspaceManager, sample_project: dict[str, Any]
) -> None:
    node = manager.resolve_node(None)
    project_id = sample_project["id"]

    default, created = manager.create(node.id)
    scratch, _created = manager.create(node.id, "scratch")
    assert created and (default.name, default.ref, scratch.ref) == ("default", 0, 1)
    existing, created_again = manager.create(node.id)
    assert (existing.id, created_again) == (default.id, False)
    manager.close(default.id)
    assert manager.create(node.id, "notes")[0].ref == 0

    first = manager.create_tab(scratch.id, pane_id=_pane_id(), project_id=project_id)
    second = manager.create_tab(scratch.id, pane_id=_pane_id(), project_id=project_id)
    assert [first.tabs[0].ref, second.tabs[0].ref] == [0, 1]
    manager.close_tab(first.tabs[0].id)
    third = manager.create_tab(scratch.id, pane_id=_pane_id(), project_id=project_id)
    assert third.tabs[0].ref == 0

    root = third.panes[0]
    split_a = manager.add_pane(_pane_id(), beside=root.id, axis="vertical").panes[0]
    split_b = manager.add_pane(_pane_id(), beside=split_a.id, axis="horizontal").panes[0]
    assert [root.ref, split_a.ref, split_b.ref] == [0, 1, 2]
    manager.remove_pane(split_a.id)
    assert manager.add_pane(_pane_id(), beside=root.id, axis="vertical").panes[0].ref == 1

    moved = manager.move_pane(split_b.id, tab_id=second.tabs[0].id).panes[0]
    assert (moved.tab_id, moved.ref) == (second.tabs[0].id, 1)
    assert manager.add_pane(_pane_id(), beside=root.id, axis="vertical").panes[0].ref == 2


def test_workspace_manager_resolves_every_reference_form(
    manager: WorkspaceManager, sample_project: dict[str, Any]
) -> None:
    node = manager.resolve_node(None)
    workspace, _created = manager.create(node.id, "main")
    created = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=sample_project["id"])
    tab, pane = created.tabs[0], created.panes[0]
    node_ref = str(node.ref)

    workspace_ids = (workspace.id, None, None)
    tab_ids = (workspace.id, tab.id, None)
    pane_ids = (workspace.id, tab.id, pane.id)
    assert _target_ids(manager.resolve_reference(workspace.id)) == workspace_ids
    assert _target_ids(manager.resolve_reference("main")) == workspace_ids
    assert _target_ids(manager.resolve_reference("0", node=node_ref)) == workspace_ids
    assert _target_ids(manager.resolve_reference(f"{node_ref}:0")) == workspace_ids
    assert _target_ids(manager.resolve_reference(tab.id)) == tab_ids
    assert _target_ids(manager.resolve_reference(f"{node_ref}:0:0")) == tab_ids
    assert _target_ids(manager.resolve_reference(pane.id)) == pane_ids
    assert _target_ids(manager.resolve_reference(f"{node_ref}:0:0:0")) == pane_ids
    assert manager.resolve_reference("main").node.ref == node.ref

    for malformed in ("0:", "a:0", "0:0:0:0:0", f"{node_ref}:0:0:0:x"):
        with pytest.raises(InvalidWorkspaceRefError):
            manager.resolve_reference(malformed)
    for missing in ("9", "absent", f"{node_ref}:0:7", str(uuid.uuid4())):
        with pytest.raises(WorkspaceNotFoundError):
            manager.resolve_reference(missing)


def test_pane_and_tab_mutations_rewrite_layouts(
    manager: WorkspaceManager, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    node = manager.resolve_node(None)
    project_id = sample_project["id"]
    workspace, _created = manager.create(node.id)
    created = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=project_id)
    tab, root = created.tabs[0], created.panes[0]
    assert tab.layout == _leaf(root.id)

    other = manager.add_pane(_pane_id(), beside=root.id, axis="vertical").panes[0]
    assert manager.set_ratio(other.id, 0.25).layout == {
        "kind": "split",
        "axis": "vertical",
        "ratio": 0.25,
        "children": [_leaf(root.id), _leaf(other.id)],
    }
    with pytest.raises(InvalidWorkspaceOpError):
        manager.set_ratio(other.id, 1.5)
    swapped = manager.swap_panes(root.id, other.id)
    assert layout_pane_ids(swapped.layout) == [other.id, root.id]
    assert manager.rename_pane(other.id, "logs").label == "logs"
    assert manager.rename_tab(tab.id, "build").title == "build"

    terminal = _terminal(TerminalManager(temp_db), project_id, live=True)
    bound = manager.set_pane_terminal(other.id, terminal.id, owns_terminal=True)
    assert bound is not None
    assert (bound.terminal_id, bound.owns_terminal) == (terminal.id, True)
    removed = manager.remove_pane(other.id)
    assert [pane.id for pane in removed.removed_panes] == [other.id]
    assert [row.layout for row in removed.tabs] == [_leaf(root.id)]
    assert manager.set_pane_terminal(other.id, terminal.id, owns_terminal=True) is None

    target, _created = manager.create(node.id, "target")
    existing = manager.create_tab(target.id, pane_id=_pane_id(), project_id=project_id).tabs[0]
    moved = manager.move_tab(tab.id, workspace_id=target.id, position=0)
    moved_tab = next(row for row in moved.tabs if row.id == tab.id)
    assert (moved_tab.workspace_id, moved_tab.ref) == (target.id, 1)
    assert [row.id for row in manager.list_tabs(target.id)] == [tab.id, existing.id]
    assert manager.list_tabs(workspace.id) == []

    emptied = manager.move_pane(root.id, tab_id=existing.id, beside=None, axis="horizontal")
    assert [row.id for row in emptied.removed_tabs] == [tab.id]
    assert layout_pane_ids(manager.list_tabs(target.id)[0].layout)[-1] == root.id


def test_workspace_rename_focus_hints_and_close(
    manager: WorkspaceManager, sample_project: dict[str, Any]
) -> None:
    node = manager.resolve_node(None)
    workspace, _created = manager.create(node.id)
    manager.create(node.id, "taken")
    created = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=sample_project["id"])
    tab, pane = created.tabs[0], created.panes[0]

    assert manager.rename(workspace.id, "main").name == "main"
    for bad_name in ("taken", "3", "0:0", " ", str(uuid.uuid4())):
        with pytest.raises(InvalidWorkspaceOpError):
            manager.rename(workspace.id, bad_name)
    with pytest.raises(InvalidWorkspaceOpError):
        manager.create(node.id, "2")

    hinted, hinted_tab = manager.set_focus_hints(
        workspace.id, project_id=sample_project["id"], tab_id=tab.id, pane_id=pane.id
    )
    assert (hinted.focused_project_id, hinted.focused_tab_id) == (sample_project["id"], tab.id)
    assert hinted_tab is not None
    assert hinted_tab.focused_pane_id == pane.id

    closed = manager.close(workspace.id)
    assert closed.id == workspace.id
    assert manager.get(workspace.id) is None
    assert [row.name for row in manager.list_for_node(node.id)] == ["taken"]
    with pytest.raises(WorkspaceNotFoundError):
        manager.close(workspace.id)


@pytest.mark.parametrize(
    "layout",
    [
        {"kind": "pane"},
        {"kind": "empty"},
        {"kind": "split", "axis": "diagonal", "ratio": 0.5, "children": []},
        {
            "kind": "split",
            "axis": "vertical",
            "ratio": 0.0,
            "children": [_leaf(str(uuid.UUID(int=1))), _leaf(str(uuid.UUID(int=2)))],
        },
        {"kind": "split", "axis": "vertical", "ratio": 0.5, "children": [_leaf("not-a-uuid")]},
        {
            "kind": "split",
            "axis": "horizontal",
            "ratio": 0.5,
            "children": [_leaf(str(uuid.UUID(int=1))), _leaf(str(uuid.UUID(int=1)))],
        },
    ],
)
def test_validate_layout_rejects_malformed_trees(layout: dict[str, Any]) -> None:
    with pytest.raises(InvalidWorkspaceOpError):
        validate_layout(layout)


def test_sweep_dead_panes_prunes_layouts(
    manager: WorkspaceManager, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminals = TerminalManager(temp_db)
    project_id = sample_project["id"]
    workspace, _created = manager.create(manager.resolve_node(None).id)

    live = _terminal(terminals, project_id, live=True)
    exited = _terminal(terminals, project_id, live=True)
    failed = _terminal(terminals, project_id, live=False)
    deleted = _terminal(terminals, project_id, live=False)
    lone = _terminal(terminals, project_id, live=True)

    main = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=project_id)
    tab, live_pane = main.tabs[0], main.panes[0]
    panes = {"live": live_pane.id}
    beside = live_pane.id
    for name in ("exited", "failed", "deleted", "in_flight"):
        panes[name] = manager.add_pane(_pane_id(), beside=beside, axis="vertical").panes[0].id
        beside = panes[name]
    for name, terminal in (
        ("live", live),
        ("exited", exited),
        ("failed", failed),
        ("deleted", deleted),
    ):
        manager.set_pane_terminal(panes[name], terminal.id, owns_terminal=True)
    lone_tab = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=project_id)
    manager.set_pane_terminal(lone_tab.panes[0].id, lone.id, owns_terminal=False)
    manager.mark_spawn_in_flight(panes["in_flight"])

    terminals.mark_exited(exited.id)
    terminals.fail_pending_attempt(
        failed.id,
        attempt_generation=failed.attempt_generation,
        attempt_started_at=failed.attempt_started_at,
    )
    temp_db.execute("DELETE FROM terminals WHERE id = %s", (deleted.id,))
    terminals.mark_exited(lone.id)

    swept = manager.sweep_dead_panes(workspace.id)
    dead = {panes[name] for name in ("exited", "failed", "deleted")}
    assert {pane.id for pane in swept.removed_panes} == dead | {lone_tab.panes[0].id}
    assert [row.id for row in swept.removed_tabs] == [lone_tab.tabs[0].id]
    assert [row.id for row in manager.list_tabs(workspace.id)] == [tab.id]
    assert layout_pane_ids(manager.list_tabs(workspace.id)[0].layout) == [
        panes["live"],
        panes["in_flight"],
    ]
    assert manager.sweep_dead_panes(workspace.id).removed_panes == ()

    manager.clear_spawn_in_flight(panes["in_flight"])
    released = manager.sweep_dead_panes(workspace.id)
    assert [pane.id for pane in released.removed_panes] == [panes["in_flight"]]
    assert manager.list_tabs(workspace.id)[0].layout == _leaf(panes["live"])

    manager.set_focus_hints(
        workspace.id, project_id=project_id, tab_id=tab.id, pane_id=panes["live"]
    )
    terminals.mark_exited(live.id)
    emptied = manager.sweep_dead_panes(workspace.id)
    assert [row.id for row in emptied.removed_tabs] == [tab.id]
    assert manager.list_tabs(workspace.id) == []
    survivor = manager.get(workspace.id)
    assert survivor is not None
    assert (survivor.name, survivor.focused_tab_id) == (workspace.name, None)


def _hints(
    manager: WorkspaceManager, workspace_id: str
) -> tuple[str | None, dict[str, str | None]]:
    """The workspace's tab hint and each tab's pane hint, as the next window sees them."""
    workspace = manager.get(workspace_id)
    assert workspace is not None
    return workspace.focused_tab_id, {
        row.id: row.focused_pane_id for row in manager.list_tabs(workspace_id)
    }


def test_pane_removals_clear_the_tab_pane_hint(
    manager: WorkspaceManager, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    workspace, _created = manager.create(manager.resolve_node(None).id)
    created = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=project_id)
    tab, root = created.tabs[0], created.panes[0]
    other = manager.add_pane(_pane_id(), beside=root.id, axis="vertical").panes[0]

    # Removing the focused pane clears the hint in the returned row; the
    # workspace's tab hint stays.
    manager.set_focus_hints(workspace.id, project_id=project_id, tab_id=tab.id, pane_id=other.id)
    assert manager.remove_pane(other.id).tabs[0].focused_pane_id is None
    assert _hints(manager, workspace.id) == (tab.id, {tab.id: None})

    # A pane that moves to another tab is no longer its source tab's focus, and a
    # hint naming a pane that stays survives its neighbour's removal.
    second = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=project_id)
    second_tab, second_pane = second.tabs[0], second.panes[0]
    moving = manager.add_pane(_pane_id(), beside=root.id, axis="horizontal").panes[0]
    manager.set_focus_hints(workspace.id, project_id=project_id, tab_id=tab.id, pane_id=moving.id)
    moved = manager.move_pane(moving.id, tab_id=second_tab.id, beside=second_pane.id)
    assert {row.id: row.focused_pane_id for row in moved.tabs} == {
        tab.id: None,
        second_tab.id: None,
    }
    manager.set_focus_hints(
        workspace.id, project_id=project_id, tab_id=second_tab.id, pane_id=second_pane.id
    )
    manager.remove_pane(moving.id)
    expected = {tab.id: None, second_tab.id: second_pane.id}
    assert _hints(manager, workspace.id) == (second_tab.id, expected)

    # The sweep clears a hint naming a dead pane the same way.
    terminals = TerminalManager(temp_db)
    dying = _terminal(terminals, project_id, live=True)
    doomed = manager.add_pane(_pane_id(), beside=root.id, axis="vertical").panes[0]
    manager.set_pane_terminal(doomed.id, dying.id, owns_terminal=True)
    for survivor in (root.id, second_pane.id):
        manager.mark_spawn_in_flight(survivor)
    manager.set_focus_hints(workspace.id, project_id=project_id, tab_id=tab.id, pane_id=doomed.id)
    terminals.mark_exited(dying.id)
    swept = manager.sweep_dead_panes(workspace.id)
    assert [(row.id, row.focused_pane_id) for row in swept.tabs] == [(tab.id, None)]
    assert _hints(manager, workspace.id) == (tab.id, expected)


def test_tab_removals_clear_the_workspace_tab_hint(
    manager: WorkspaceManager, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    node = manager.resolve_node(None)
    workspace, _created = manager.create(node.id)

    def open_tab() -> tuple[str, str]:
        created = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=project_id)
        return created.tabs[0].id, created.panes[0].id

    def focus(tab_id: str, pane_id: str) -> None:
        manager.set_focus_hints(workspace.id, project_id=project_id, tab_id=tab_id, pane_id=pane_id)

    # Closing the focused tab clears the hint; a hint naming another tab survives
    # a tab removal, including one that empties the tab pane by pane.
    closing, closing_pane = open_tab()
    kept, kept_pane = open_tab()
    focus(closing, closing_pane)
    manager.close_tab(closing)
    assert _hints(manager, workspace.id) == (None, {kept: None})
    focus(kept, kept_pane)
    _emptied, emptied_pane = open_tab()
    manager.remove_pane(emptied_pane)
    assert _hints(manager, workspace.id) == (kept, {kept: kept_pane})
    manager.remove_pane(kept_pane)
    assert _hints(manager, workspace.id) == (None, {})

    # A tab moved to another workspace seeds neither workspace; a move within
    # its workspace keeps the hint.
    moving, moving_pane = open_tab()
    focus(moving, moving_pane)
    target, _created = manager.create(node.id, "target")
    manager.move_tab(moving, workspace_id=target.id, position=0)
    assert _hints(manager, workspace.id) == (None, {})
    assert _hints(manager, target.id) == (None, {moving: moving_pane})
    manager.set_focus_hints(target.id, project_id=project_id, tab_id=moving, pane_id=moving_pane)
    manager.move_tab(moving, workspace_id=target.id, position=0)
    assert _hints(manager, target.id) == (moving, {moving: moving_pane})


@dataclass(frozen=True)
class _Seat:
    """A workspace whose first tab holds a settled root pane beside a hot pane."""

    workspace_id: str
    tab_id: str
    root: str
    hot: str
    other_tab: str


def _seat(manager: WorkspaceManager, project_id: str) -> _Seat:
    workspace, _created = manager.create(manager.resolve_node(None).id, f"seat-{_pane_id()}")
    created = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=project_id)
    tab, root = created.tabs[0], created.panes[0]
    hot = _pane_id()
    manager.mark_spawn_in_flight(hot)
    manager.add_pane(hot, beside=root.id, axis="vertical")
    other = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=project_id)
    return _Seat(workspace.id, tab.id, root.id, hot, other.tabs[0].id)


def _rows(manager: WorkspaceManager, workspace_id: str) -> tuple[object, ...]:
    return (
        manager.get(workspace_id),
        tuple(manager.list_tabs(workspace_id)),
        tuple(manager.list_panes(workspace_id)),
    )


_GUARDED: dict[str, Callable[[WorkspaceManager, _Seat], object]] = {
    "close": lambda m, s: m.close(s.workspace_id, refuse_in_flight=True),
    "close_tab": lambda m, s: m.close_tab(s.tab_id, refuse_in_flight=True),
    "remove_pane": lambda m, s: m.remove_pane(s.hot, refuse_in_flight=True),
    "move_pane": lambda m, s: m.move_pane(s.hot, tab_id=s.other_tab, refuse_in_flight=True),
    "move_tab": lambda m, s: m.move_tab(
        s.tab_id, workspace_id=s.workspace_id, position=1, refuse_in_flight=True
    ),
    "swap_panes": lambda m, s: m.swap_panes(s.root, s.hot, refuse_in_flight=True),
}


@pytest.mark.parametrize("hold", ["unbound", "bound", "in_doubt"])
@pytest.mark.parametrize("mutation", sorted(_GUARDED))
def test_guarded_mutations_refuse_in_flight_panes(
    manager: WorkspaceManager,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    mutation: str,
    hold: str,
) -> None:
    seat = _seat(manager, sample_project["id"])
    terminal = _terminal(TerminalManager(temp_db), sample_project["id"], live=False)
    if hold != "unbound":
        manager.set_pane_terminal(seat.hot, terminal.id, owns_terminal=True)
    if hold == "in_doubt":
        # The owner claims the id while the mark is still set, then the mark clears.
        assert in_doubt_spawns.claim(terminal.id)
        manager.clear_spawn_in_flight(seat.hot)
    before = _rows(manager, seat.workspace_id)
    try:
        with pytest.raises(WorkspaceBusyError):
            _GUARDED[mutation](manager, seat)
        assert _rows(manager, seat.workspace_id) == before
    finally:
        in_doubt_spawns.release(terminal.id)
    # The unguarded default is how a reservation removes its own pane.
    assert [pane.id for pane in manager.remove_pane(seat.hot).removed_panes] == [seat.hot]


@dataclass(frozen=True)
class _Hold:
    """Where the first call parks: after ``name`` on ``owner`` returns, or before it runs."""

    owner: object
    name: str
    after: bool = True


_AFTER_GUARD = _Hold(WorkspaceManager, "_guard")
_AFTER_INSERT = _Hold(workspaces_module, "_insert_pane")


def _wait_for_lock_waiter(db: PostgresHubDatabase, contender: Thread) -> None:
    deadline = time.monotonic() + 5.0
    with psycopg.connect(db.conninfo) as monitor:
        while time.monotonic() < deadline:
            if not contender.is_alive():
                raise AssertionError("the contender finished instead of waiting on a lock")
            row = monitor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND application_name = %s AND wait_event_type = 'Lock'",
                (db.application_name,),
            ).fetchone()
            if row is not None and int(row[0]) >= 1:
                return
            time.sleep(0.02)
    raise AssertionError("timed out waiting for the contender to block on a row lock")


def _race(
    db: PostgresHubDatabase,
    hold: _Hold,
    first: Callable[[], object],
    second: Callable[[], object],
    *,
    second_waits: bool = True,
) -> tuple[Exception | None, Exception | None]:
    """Park ``first`` inside its transaction, run ``second`` on its own connection, release.

    With ``second_waits`` the contender must block on a row lock ``first`` holds;
    without it the contender must finish while ``first`` is still parked. Returns
    each call's exception.
    """
    original = getattr(hold.owner, hold.name)
    parked, release = Event(), Event()
    errors: dict[str, Exception | None] = {"first": None, "second": None}

    def held(*args: Any, **kwargs: Any) -> object:
        if current_thread().name != "first":
            return original(*args, **kwargs)
        result = original(*args, **kwargs) if hold.after else None
        parked.set()
        if not release.wait(10):
            raise AssertionError("the race was never released")
        return result if hold.after else original(*args, **kwargs)

    def thread(name: str, call: Callable[[], object]) -> Thread:
        def run() -> None:
            try:
                call()
            except Exception as exc:
                errors[name] = exc

        return Thread(target=run, name=name, daemon=True)

    one, two = thread("first", first), thread("second", second)
    with patch.object(hold.owner, hold.name, held):
        try:
            one.start()
            assert parked.wait(10), "the first call never reached its hold point"
            two.start()
            if second_waits:
                _wait_for_lock_waiter(db, two)
            else:
                two.join(5)
                assert not two.is_alive(), "the contender blocked behind the parked call"
        finally:
            release.set()
            one.join(10)
            two.join(10)
    assert not one.is_alive() and not two.is_alive()
    return errors["first"], errors["second"]


@pytest.mark.parametrize("first", ["insert", "guard"])
def test_guard_and_insert_serialize(
    manager: WorkspaceManager,
    temp_db: PostgresHubDatabase,
    sample_project: dict[str, Any],
    first: str,
) -> None:
    project_id = sample_project["id"]
    workspace, _created = manager.create(manager.resolve_node(None).id)
    created = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=project_id)
    tab, root = created.tabs[0], created.panes[0]
    agent = _pane_id()
    manager.mark_spawn_in_flight(agent)

    def insert() -> None:
        manager.add_pane(
            agent,
            beside=root.id,
            axis="vertical",
            expected_workspace_id=workspace.id,
            expected_tab_id=tab.id,
        )

    def guard() -> None:
        manager.close_tab(tab.id, refuse_in_flight=True, expected_panes={root.id: None})

    if first == "insert":
        errors = _race(temp_db, _AFTER_INSERT, insert, guard)
        assert errors[0] is None
        assert isinstance(errors[1], WorkspaceBusyError)
        assert {pane.id for pane in manager.list_panes(workspace.id)} == {root.id, agent}
    else:
        errors = _race(temp_db, _AFTER_GUARD, guard, insert)
        assert errors[0] is None
        assert isinstance(errors[1], WorkspaceNotFoundError)
        assert manager.list_panes(workspace.id) == []
        assert _pane_row(manager, agent) is None


def _pane_row(manager: WorkspaceManager, pane_id: str) -> object:
    return manager.db.fetchone("SELECT id FROM workspace_panes WHERE id = %s", (pane_id,))


@pytest.mark.parametrize("drift", ["pane_moved", "tab_moved", "other_project"])
def test_add_pane_refuses_moved_beside_target(
    manager: WorkspaceManager, sample_project: dict[str, Any], drift: str
) -> None:
    project_id = sample_project["id"]
    node = manager.resolve_node(None)
    workspace, _created = manager.create(node.id)
    created = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=project_id)
    tab, beside = created.tabs[0], created.panes[0]
    anchor = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=project_id)
    expected_project = project_id
    if drift == "pane_moved":
        manager.move_pane(beside.id, tab_id=anchor.tabs[0].id)
    elif drift == "tab_moved":
        target, _created = manager.create(node.id, "elsewhere")
        manager.move_tab(tab.id, workspace_id=target.id, position=0)
    else:
        expected_project = str(uuid.uuid4())
    before = {ws.id: _rows(manager, ws.id) for ws in manager.list_for_node(node.id)}
    agent = _pane_id()
    with pytest.raises(WorkspaceNotFoundError):
        manager.add_pane(
            agent,
            beside=beside.id,
            axis="vertical",
            expected_workspace_id=workspace.id,
            expected_tab_id=tab.id,
            expected_project_id=expected_project,
        )
    assert _pane_row(manager, agent) is None
    assert {ws.id: _rows(manager, ws.id) for ws in manager.list_for_node(node.id)} == before


@pytest.mark.parametrize("first", ["close", "insert"])
def test_empty_workspace_close_serializes_with_new_tab(
    manager: WorkspaceManager,
    temp_db: PostgresHubDatabase,
    sample_project: dict[str, Any],
    first: str,
) -> None:
    workspace, _created = manager.create(manager.resolve_node(None).id)
    agent = _pane_id()
    manager.mark_spawn_in_flight(agent)

    def insert() -> None:
        manager.create_tab(workspace.id, pane_id=agent, project_id=sample_project["id"])

    def close() -> None:
        manager.close(workspace.id, refuse_in_flight=True, expected_panes={})

    if first == "close":
        errors = _race(temp_db, _AFTER_GUARD, close, insert)
        assert errors[0] is None
        assert isinstance(errors[1], WorkspaceNotFoundError)
        assert manager.get(workspace.id) is None
        assert _pane_row(manager, agent) is None
    else:
        errors = _race(temp_db, _AFTER_INSERT, insert, close)
        assert errors[0] is None
        assert isinstance(errors[1], WorkspaceBusyError)
        assert [pane.id for pane in manager.list_panes(workspace.id)] == [agent]


def test_add_pane_with_stale_workspace_never_locks_against_a_reverse_move(
    manager: WorkspaceManager, temp_db: PostgresHubDatabase, sample_project: dict[str, Any]
) -> None:
    node = manager.resolve_node(None).id
    low, high = sorted(
        (manager.create(node, f"lock-{_pane_id()}")[0] for _ in range(2)), key=lambda ws: ws.id
    )
    created = manager.create_tab(high.id, pane_id=_pane_id(), project_id=sample_project["id"])
    tab, root = created.tabs[0], created.panes[0]
    manager.move_tab(tab.id, workspace_id=low.id, position=0)
    agent = _pane_id()
    manager.mark_spawn_in_flight(agent)

    # Preflight read the tab in ``high``; it now sits in ``low``, which sorts first.
    def insert() -> None:
        manager.add_pane(
            agent,
            beside=root.id,
            axis="vertical",
            expected_workspace_id=high.id,
            expected_tab_id=tab.id,
        )

    def move_back() -> None:
        manager.move_tab(tab.id, workspace_id=high.id, position=0)

    errors = _race(
        temp_db,
        _Hold(workspaces_module, "_lock_pane_tabs", after=False),
        insert,
        move_back,
        second_waits=False,
    )
    assert errors == (None, None)
    assert {pane.id for pane in manager.list_panes(high.id)} == {root.id, agent}
    assert manager.list_panes(low.id) == []


def test_sweep_keeps_orphaned_panes(
    manager: WorkspaceManager, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminals = TerminalManager(temp_db)
    project_id = sample_project["id"]
    workspace, _created = manager.create(manager.resolve_node(None).id)
    seat = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=project_id).panes[0]
    terminal = _terminal(terminals, project_id, live=True)
    manager.set_pane_terminal(seat.id, terminal.id, owns_terminal=True)

    assert terminals.mark_orphaned(terminal.id) is not None
    assert manager.sweep_dead_panes(workspace.id).removed_panes == ()
    assert [pane.id for pane in manager.list_panes(workspace.id)] == [seat.id]

    assert terminals.mark_exited(terminal.id) is not None
    swept = manager.sweep_dead_panes(workspace.id)
    assert [pane.id for pane in swept.removed_panes] == [seat.id]
    assert manager.list_panes(workspace.id) == []
