from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._automation import HOLD_LABELS
from gobby.storage.tasks._stage_utils import close_eligible_parent_chain
from gobby.storage.tasks._transitions import claim_task
from gobby.utils.machine_id import get_machine_id
from tests.storage.tasks._stage_test_helpers import (
    initialize_manifest,
    set_stage_state,
    spec,
)

pytestmark = pytest.mark.unit


def test_last_sibling_close_closes_parent(temp_db: HubDatabase, tmp_path: Path) -> None:
    manager, project_id = _manager(temp_db, tmp_path)
    parent = _create(manager, project_id, "Parent", task_type="epic")
    first = _create(manager, project_id, "First", parent_task_id=parent.id)
    second = _create(manager, project_id, "Second", parent_task_id=parent.id)

    manager.close_task(first.id)
    assert _open(manager, parent.id)

    manager.close_task(second.id)
    assert not _open(manager, parent.id)


def test_three_level_last_leaf_closes_phase_and_epic(temp_db: HubDatabase, tmp_path: Path) -> None:
    manager, project_id = _manager(temp_db, tmp_path)
    epic = _create(manager, project_id, "Epic", task_type="epic")
    phase = _create(manager, project_id, "Phase", task_type="epic", parent_task_id=epic.id)
    leaf = _create(manager, project_id, "Leaf", parent_task_id=phase.id)

    closed_ancestors: list[str] = []
    manager.close_task(leaf.id, closed_ancestors=closed_ancestors)

    assert closed_ancestors == [phase.id, epic.id]
    assert not _open(manager, phase.id)
    assert not _open(manager, epic.id)


def test_open_cousin_stops_ancestor_walk(temp_db: HubDatabase, tmp_path: Path) -> None:
    manager, project_id = _manager(temp_db, tmp_path)
    epic = _create(manager, project_id, "Epic", task_type="epic")
    phase_one = _create(manager, project_id, "P1", task_type="epic", parent_task_id=epic.id)
    phase_two = _create(manager, project_id, "P2", task_type="epic", parent_task_id=epic.id)
    leaf_one = _create(manager, project_id, "L1", parent_task_id=phase_one.id)
    _create(manager, project_id, "L2", parent_task_id=phase_two.id)

    closed_ancestors: list[str] = []
    manager.close_task(leaf_one.id, closed_ancestors=closed_ancestors)

    assert closed_ancestors == [phase_one.id]
    assert not _open(manager, phase_one.id)
    assert _open(manager, epic.id)
    assert _open(manager, phase_two.id)


def test_childless_epic_closes_without_ledger(temp_db: HubDatabase, tmp_path: Path) -> None:
    manager, project_id = _manager(temp_db, tmp_path)
    epic = _create(manager, project_id, "Empty epic", task_type="epic")

    closed = manager.close_task(epic.id)

    assert closed.closed_at is not None


def test_force_close_auto_closes_parent_without_open_siblings(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    manager, project_id = _manager(temp_db, tmp_path)
    grand = _create(manager, project_id, "Grand", task_type="epic")
    parent = _create(manager, project_id, "Parent", task_type="epic", parent_task_id=grand.id)
    _create(manager, project_id, "Still open", parent_task_id=parent.id)

    closed_ancestors: list[str] = []
    manager.close_task(parent.id, force=True, closed_ancestors=closed_ancestors)

    assert not _open(manager, parent.id)
    assert closed_ancestors == [grand.id]
    assert not _open(manager, grand.id)


def _manager(temp_db: HubDatabase, _tmp_path: Path) -> tuple[LocalTaskManager, str]:
    project = LocalProjectManager(temp_db).create(
        name="ancestor-close",
    )
    return LocalTaskManager(temp_db), project.id


def _create(
    manager: LocalTaskManager,
    project_id: str,
    title: str,
    **kwargs: Any,
) -> Task:
    kwargs.setdefault("validation_criteria", "Observable completion.")
    return manager.create_task(
        project_id,
        title=title,
        **kwargs,
    )


def _open(manager: LocalTaskManager, task_id: str) -> bool:
    task = manager.get_task(task_id)
    assert task is not None
    return task.closed_at is None


def test_parent_with_unfinished_stages_survives_its_last_child(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    """A delivery manifest is work the parent still owes.

    Closing it on the last child's close ships the epic without its epic_qa,
    pr, or merge stage ever running, and strands those manifest rows on a
    closed task. The merge stage is what closes such an epic.
    """
    manager, project_id = _manager(temp_db, tmp_path)
    parent = _create(manager, project_id, "Delivery epic", task_type="epic")
    leaf = _create(manager, project_id, "Only leaf", parent_task_id=parent.id)
    initialize_manifest(temp_db, parent.id, [spec("epic_qa", 0), spec("pr", 1), spec("merge", 2)])

    closed_ancestors: list[str] = []
    manager.close_task(leaf.id, closed_ancestors=closed_ancestors)

    assert closed_ancestors == []
    assert _open(manager, parent.id)


def test_parent_with_a_finished_manifest_still_closes(temp_db: HubDatabase, tmp_path: Path) -> None:
    manager, project_id = _manager(temp_db, tmp_path)
    parent = _create(manager, project_id, "Delivered epic", task_type="epic")
    leaf = _create(manager, project_id, "Only leaf", parent_task_id=parent.id)
    initialize_manifest(temp_db, parent.id, [spec("epic_qa", 0), spec("merge", 1)])
    for stage_name in ("epic_qa", "merge"):
        set_stage_state(temp_db, parent.id, stage_name, "done")

    closed_ancestors: list[str] = []
    manager.close_task(leaf.id, closed_ancestors=closed_ancestors)

    assert closed_ancestors == [parent.id]
    assert not _open(manager, parent.id)


def test_claimed_parent_survives_its_last_child(temp_db: HubDatabase, tmp_path: Path) -> None:
    """A claimed ancestor is in-flight work its owner closes through the leaf gates.

    Found-work child #21046 closed under the claimed, uncommitted leaf #20969
    and the walk auto-closed #20969 and its epic, skipping every gate and
    dropping the claim.
    """
    manager, project_id = _manager(temp_db, tmp_path)
    epic = _create(manager, project_id, "Epic", task_type="epic")
    parent = _create(manager, project_id, "Worked leaf", parent_task_id=epic.id)
    child = _create(manager, project_id, "Found-work child", parent_task_id=parent.id)
    owner = SessionManager(temp_db).register(
        external_id="owner-session",
        machine_id=get_machine_id(),
        source="codex",
        project_id=project_id,
    )
    claim_task(temp_db, parent.id, owner.id)

    closed_ancestors: list[str] = []
    manager.close_task(child.id, closed_ancestors=closed_ancestors)

    assert closed_ancestors == []
    assert _open(manager, parent.id)
    assert _open(manager, epic.id)
    reloaded = manager.get_task(parent.id)
    assert reloaded is not None
    assert reloaded.claimed_by_session_id == owner.id


@pytest.mark.parametrize("hold_label", sorted(HOLD_LABELS))
def test_hold_label_parent_survives_its_last_child(
    temp_db: HubDatabase, tmp_path: Path, hold_label: str
) -> None:
    """A hold label marks unresolved owner action a parent still owes (#22421).

    Auto-closing the parent on its last child's close removed the open owner
    decision from open lists; the parent closes through its own gates instead.
    """
    manager, project_id = _manager(temp_db, tmp_path)
    epic = _create(manager, project_id, "Epic", task_type="epic")
    parent = _create(
        manager,
        project_id,
        "Held parent",
        parent_task_id=epic.id,
        labels=[hold_label],
    )
    leaf = _create(manager, project_id, "Only leaf", parent_task_id=parent.id)

    closed_ancestors: list[str] = []
    manager.close_task(leaf.id, closed_ancestors=closed_ancestors)

    assert closed_ancestors == []
    assert _open(manager, parent.id)
    assert _open(manager, epic.id)


def test_hold_label_epic_survives_its_last_child(temp_db: HubDatabase, tmp_path: Path) -> None:
    """An epic's own criteria never gate auto-close, but its hold label does."""
    manager, project_id = _manager(temp_db, tmp_path)
    epic = _create(manager, project_id, "Held epic", task_type="epic", labels=["needs-decision"])
    leaf = _create(manager, project_id, "Only leaf", parent_task_id=epic.id)

    closed_ancestors: list[str] = []
    manager.close_task(leaf.id, closed_ancestors=closed_ancestors)

    assert closed_ancestors == []
    assert _open(manager, epic.id)


def test_criteria_owning_parent_survives_its_last_child(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    """A non-epic parent owns acceptance criteria only its own close gates verify.

    The ancestor walk never checks them, so it leaves the parent for the close
    flow that does (#22421).
    """
    manager, project_id = _manager(temp_db, tmp_path)
    epic = _create(manager, project_id, "Epic", task_type="epic")
    parent = _create(
        manager,
        project_id,
        "Decision owner",
        parent_task_id=epic.id,
        validation_criteria="The owner explicitly accepts the delivered phase.",
    )
    leaf = _create(manager, project_id, "Only leaf", parent_task_id=parent.id)

    closed_ancestors: list[str] = []
    manager.close_task(leaf.id, closed_ancestors=closed_ancestors)

    assert closed_ancestors == []
    assert _open(manager, parent.id)
    assert _open(manager, epic.id)


def test_reparent_that_empties_the_old_parent_closes_it(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    """Moving the last open child out closes the old parent and its emptied ancestors.

    #22508 kept 33 closed children open for days: its last open child was moved
    to another lane, and no child close ever arrived to run the walk (#23384).
    """
    manager, project_id = _manager(temp_db, tmp_path)
    grand = _create(manager, project_id, "Grand epic", task_type="epic")
    old_parent = _create(manager, project_id, "Old epic", task_type="epic", parent_task_id=grand.id)
    done = _create(manager, project_id, "Done child", parent_task_id=old_parent.id)
    moved = _create(manager, project_id, "Moved child", parent_task_id=old_parent.id)
    new_parent = _create(manager, project_id, "New epic", task_type="epic")
    manager.close_task(done.id)
    assert _open(manager, old_parent.id)

    manager.update_task(moved.id, parent_task_id=new_parent.id)

    assert not _open(manager, old_parent.id)
    assert not _open(manager, grand.id)
    assert _open(manager, new_parent.id)
    assert _open(manager, moved.id)


@pytest.mark.parametrize("guard", ["open_sibling", "hold_label", "claimed"])
def test_reparent_keeps_a_guarded_old_parent_open(
    temp_db: HubDatabase, tmp_path: Path, guard: str
) -> None:
    """A reparent runs the same walk as a child close, with every guard intact."""
    manager, project_id = _manager(temp_db, tmp_path)
    labels = ["needs-decision"] if guard == "hold_label" else None
    old_parent = _create(manager, project_id, "Old epic", task_type="epic", labels=labels)
    moved = _create(manager, project_id, "Moved child", parent_task_id=old_parent.id)
    new_parent = _create(manager, project_id, "New epic", task_type="epic")
    if guard == "open_sibling":
        _create(manager, project_id, "Open sibling", parent_task_id=old_parent.id)
    if guard == "claimed":
        owner = SessionManager(temp_db).register(
            external_id="owner-session",
            machine_id=get_machine_id(),
            source="codex",
            project_id=project_id,
        )
        claim_task(temp_db, old_parent.id, owner.id)

    manager.update_task(moved.id, parent_task_id=new_parent.id)

    assert _open(manager, old_parent.id)


def test_inverse_concurrent_reparents_do_not_deadlock(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two moves in opposite directions both reach the old-parent walk together.

    Run inside the move's transaction, each walk waited on the old parent row while
    holding its proposed parent row, which the other move held the other way round.
    """
    manager, project_id = _manager(temp_db, tmp_path)
    left = _create(manager, project_id, "Left epic", task_type="epic")
    right = _create(manager, project_id, "Right epic", task_type="epic")
    from_left = _create(manager, project_id, "From left", parent_task_id=left.id)
    from_right = _create(manager, project_id, "From right", parent_task_id=right.id)

    both_at_walk = threading.Barrier(2, timeout=10.0)

    def walk_together(*args: Any, **kwargs: Any) -> None:
        both_at_walk.wait()
        close_eligible_parent_chain(*args, **kwargs)

    monkeypatch.setattr("gobby.storage.tasks._manager.close_eligible_parent_chain", walk_together)

    with ThreadPoolExecutor(max_workers=2) as executor:
        moves = [
            executor.submit(manager.update_task, from_left.id, parent_task_id=right.id),
            executor.submit(manager.update_task, from_right.id, parent_task_id=left.id),
        ]
        for move in moves:
            move.result(timeout=30.0)

    moved_right = manager.get_task(from_left.id)
    moved_left = manager.get_task(from_right.id)
    assert moved_right is not None and moved_right.parent_task_id == right.id
    assert moved_left is not None and moved_left.parent_task_id == left.id
    assert _open(manager, left.id)
    assert _open(manager, right.id)
