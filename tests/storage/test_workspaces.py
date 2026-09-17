"""WorkspaceManager storage contract (plan gclient-workspaces 1.2)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.terminals import Terminal, TerminalManager, native_locator_key
from gobby.storage.workspaces import (
    InvalidWorkspaceOpError,
    InvalidWorkspaceRefError,
    WorkspaceManager,
    WorkspaceNotFoundError,
    WorkspaceTarget,
    layout_pane_ids,
    validate_layout,
)
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
    assert created and (default.name, default.ref, scratch.ref) == ("default", 1, 2)
    existing, created_again = manager.create(node.id)
    assert (existing.id, created_again) == (default.id, False)
    manager.close(default.id)
    assert manager.create(node.id, "notes")[0].ref == 1

    first = manager.create_tab(scratch.id, pane_id=_pane_id(), project_id=project_id)
    second = manager.create_tab(scratch.id, pane_id=_pane_id(), project_id=project_id)
    assert [first.tabs[0].ref, second.tabs[0].ref] == [1, 2]
    manager.close_tab(first.tabs[0].id)
    third = manager.create_tab(scratch.id, pane_id=_pane_id(), project_id=project_id)
    assert third.tabs[0].ref == 1

    root = third.panes[0]
    split_a = manager.add_pane(_pane_id(), beside=root.id, axis="vertical").panes[0]
    split_b = manager.add_pane(_pane_id(), beside=split_a.id, axis="horizontal").panes[0]
    assert [root.ref, split_a.ref, split_b.ref] == [1, 2, 3]
    manager.remove_pane(split_a.id)
    assert manager.add_pane(_pane_id(), beside=root.id, axis="vertical").panes[0].ref == 2

    moved = manager.move_pane(split_b.id, tab_id=second.tabs[0].id).panes[0]
    assert (moved.tab_id, moved.ref) == (second.tabs[0].id, 2)
    assert manager.add_pane(_pane_id(), beside=root.id, axis="vertical").panes[0].ref == 3


def test_workspace_manager_resolves_every_reference_form(
    manager: WorkspaceManager, sample_project: dict[str, Any]
) -> None:
    node = manager.resolve_node(None)
    workspace, _created = manager.create(node.id, "main")
    created = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=sample_project["id"])
    tab, pane = created.tabs[0], created.panes[0]
    node_ref = f"n{node.ref}"

    workspace_ids = (workspace.id, None, None)
    tab_ids = (workspace.id, tab.id, None)
    pane_ids = (workspace.id, tab.id, pane.id)
    assert _target_ids(manager.resolve_reference(workspace.id)) == workspace_ids
    assert _target_ids(manager.resolve_reference("main")) == workspace_ids
    assert _target_ids(manager.resolve_reference("w1", node=node_ref)) == workspace_ids
    assert _target_ids(manager.resolve_reference(f"{node_ref}:w1")) == workspace_ids
    assert _target_ids(manager.resolve_reference(tab.id)) == tab_ids
    assert _target_ids(manager.resolve_reference(f"{node_ref}:w1:t1")) == tab_ids
    assert _target_ids(manager.resolve_reference(pane.id)) == pane_ids
    assert _target_ids(manager.resolve_reference(f"{node_ref}:w1:t1:p1")) == pane_ids
    assert manager.resolve_reference("main").node.ref == node.ref

    for malformed in ("w0", "n1:t1", "w1:p1", f"{node_ref}:w1:t1:p1:x"):
        with pytest.raises(InvalidWorkspaceRefError):
            manager.resolve_reference(malformed)
    for missing in ("w9", "absent", f"{node_ref}:w1:t7", str(uuid.uuid4())):
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
    assert (moved_tab.workspace_id, moved_tab.ref) == (target.id, 2)
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
    for bad_name in ("taken", "w3", "n1:w1", " ", str(uuid.uuid4())):
        with pytest.raises(InvalidWorkspaceOpError):
            manager.rename(workspace.id, bad_name)
    with pytest.raises(InvalidWorkspaceOpError):
        manager.create(node.id, "t2")

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
    orphaned = _terminal(terminals, project_id, live=True)
    failed = _terminal(terminals, project_id, live=False)
    deleted = _terminal(terminals, project_id, live=False)
    lone = _terminal(terminals, project_id, live=True)

    main = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=project_id)
    tab, live_pane = main.tabs[0], main.panes[0]
    panes = {"live": live_pane.id}
    beside = live_pane.id
    for name in ("exited", "orphaned", "failed", "deleted", "in_flight"):
        panes[name] = manager.add_pane(_pane_id(), beside=beside, axis="vertical").panes[0].id
        beside = panes[name]
    for name, terminal in (
        ("live", live),
        ("exited", exited),
        ("orphaned", orphaned),
        ("failed", failed),
        ("deleted", deleted),
    ):
        manager.set_pane_terminal(panes[name], terminal.id, owns_terminal=True)
    lone_tab = manager.create_tab(workspace.id, pane_id=_pane_id(), project_id=project_id)
    manager.set_pane_terminal(lone_tab.panes[0].id, lone.id, owns_terminal=False)
    manager.mark_spawn_in_flight(panes["in_flight"])

    terminals.mark_exited(exited.id)
    terminals.mark_orphaned(orphaned.id)
    terminals.fail_pending_attempt(
        failed.id,
        attempt_generation=failed.attempt_generation,
        attempt_started_at=failed.attempt_started_at,
    )
    temp_db.execute("DELETE FROM terminals WHERE id = %s", (deleted.id,))
    terminals.mark_exited(lone.id)

    swept = manager.sweep_dead_panes(workspace.id)
    dead = {panes[name] for name in ("exited", "orphaned", "failed", "deleted")}
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

    terminals.mark_exited(live.id)
    emptied = manager.sweep_dead_panes(workspace.id)
    assert [row.id for row in emptied.removed_tabs] == [tab.id]
    assert manager.list_tabs(workspace.id) == []
    survivor = manager.get(workspace.id)
    assert survivor is not None
    assert survivor.name == workspace.name
