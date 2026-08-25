"""Scoping contract for the cumulative-epic-guard task fetch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._guard_scope import list_epic_guard_scope
from gobby.tasks.epic_guards import collect_epic_guard_paths
from tests.storage.tasks._stage_test_helpers import create_task

pytestmark = pytest.mark.unit


def test_scope_holds_the_epic_subtree_and_the_ancestor_chain(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    epic = create_task(temp_db, sample_project, title="Epic", task_type="epic", category="planning")
    middle = create_task(
        temp_db, sample_project, title="Middle", parent_task_id=epic.id, category="code"
    )
    leaf = create_task(
        temp_db, sample_project, title="Leaf", parent_task_id=middle.id, category="code"
    )
    sibling = create_task(
        temp_db, sample_project, title="Sibling", parent_task_id=epic.id, category="code"
    )

    scope = {task.id for task in list_epic_guard_scope(temp_db, leaf.id)}

    assert scope == {epic.id, middle.id, leaf.id, sibling.id}


def test_scope_does_not_grow_with_unrelated_project_tasks(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Adding tasks outside the epic must not enlarge what guard collection reads.

    The previous implementation paged every task in the project and discarded
    all but the subtree, so its cost tracked project size rather than epic size
    (#20847).
    """
    epic = create_task(temp_db, sample_project, title="Epic", task_type="epic", category="planning")
    leaf = create_task(
        temp_db, sample_project, title="Leaf", parent_task_id=epic.id, category="code"
    )

    before = len(list_epic_guard_scope(temp_db, leaf.id))
    other_epic = create_task(
        temp_db, sample_project, title="Other", task_type="epic", category="planning"
    )
    for index in range(25):
        create_task(
            temp_db,
            sample_project,
            title=f"Unrelated {index}",
            parent_task_id=other_epic.id,
            category="code",
        )

    assert len(list_epic_guard_scope(temp_db, leaf.id)) == before


def test_scope_stops_at_the_nearest_epic(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    outer = create_task(
        temp_db, sample_project, title="Outer", task_type="epic", category="planning"
    )
    outer_child = create_task(
        temp_db, sample_project, title="Outer child", parent_task_id=outer.id, category="code"
    )
    inner = create_task(
        temp_db,
        sample_project,
        title="Inner",
        task_type="epic",
        parent_task_id=outer.id,
        category="planning",
    )
    leaf = create_task(
        temp_db, sample_project, title="Leaf", parent_task_id=inner.id, category="code"
    )

    scope = {task.id for task in list_epic_guard_scope(temp_db, leaf.id)}

    assert inner.id in scope
    assert leaf.id in scope
    assert outer_child.id not in scope, "the outer epic's other branch is out of scope"
    assert outer.id in scope, "ancestors stay in scope so nearest-epic resolution can walk them"


def test_scope_without_an_epic_ancestor_is_the_ancestor_chain(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    root = create_task(temp_db, sample_project, title="Root", category="code")
    leaf = create_task(
        temp_db, sample_project, title="Leaf", parent_task_id=root.id, category="code"
    )
    create_task(temp_db, sample_project, title="Elsewhere", category="code")

    scope = {task.id for task in list_epic_guard_scope(temp_db, leaf.id)}

    assert scope == {root.id, leaf.id}


def test_guard_collection_reads_the_scoped_rows_end_to_end(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Every field guard collection reads must survive the scoped query.

    The behaviour tests drive collection through an in-memory double, so they
    would still pass if the query dropped a column the guard depends on. This
    runs the real fetch: `validation_criteria` and `closed_at` come off the row,
    and a sibling that is still open or has children must not count as a prior
    closed leaf.
    """
    guard = Path(tmp_path, "tests", "test_scoped_guard.py")
    guard.parent.mkdir()
    guard.write_text("def test_scoped_guard(): pass\n", encoding="utf-8")
    manager = LocalTaskManager(temp_db)
    epic = create_task(temp_db, sample_project, title="Epic", task_type="epic", category="planning")
    prior = manager.create_task(
        project_id=sample_project["id"],
        title="Prior leaf",
        parent_task_id=epic.id,
        category="code",
        validation_criteria="Covered. test: `tests/test_scoped_guard.py::test_scoped_guard`.",
    )
    still_open = manager.create_task(
        project_id=sample_project["id"],
        title="Open sibling",
        parent_task_id=epic.id,
        category="code",
        validation_criteria="Covered. test: `tests/test_open_sibling.py::test_open`.",
    )
    outside = manager.create_task(
        project_id=sample_project["id"],
        title="Outside the epic",
        category="code",
        validation_criteria="Covered. test: `tests/test_outside.py::test_outside`.",
    )
    _mark_closed(temp_db, prior.id)
    _mark_closed(temp_db, outside.id)
    current = create_task(
        temp_db, sample_project, title="Current", parent_task_id=epic.id, category="code"
    )

    paths, sources, errors, _deleted = collect_epic_guard_paths(
        task_manager=manager,
        task=manager.get_task(current.id),
        repo_path=str(tmp_path),
    )

    assert paths == ("tests/test_scoped_guard.py",)
    assert sources == (prior.id,)
    assert errors == ()
    assert still_open.id not in sources
    assert outside.id not in sources


def _mark_closed(db: HubDatabase, task_id: str) -> None:
    """Stamp closed_at directly.

    `update_task` refuses lifecycle fields and routes callers to `close_task`,
    which would run the very guard evaluation under test.
    """
    db.execute(
        "UPDATE tasks SET closed_at = %s WHERE id = %s",
        ("2026-08-24T00:00:00+00:00", task_id),
    )


def test_manager_exposes_the_scoped_fetch(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    epic = create_task(temp_db, sample_project, title="Epic", task_type="epic", category="planning")
    leaf = create_task(
        temp_db, sample_project, title="Leaf", parent_task_id=epic.id, category="code"
    )

    scope = {task.id for task in LocalTaskManager(temp_db).list_epic_guard_scope(leaf.id)}

    assert scope == {epic.id, leaf.id}
