"""Regression coverage for closing legacy tasks without validation criteria."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._stage_utils import _close_task_in_txn

pytestmark = pytest.mark.unit

LEGACY_VALIDATION_CRITERIA = (
    "Legacy task: validation criteria were not recorded before they became required."
)

_ADD_VALIDATION_CRITERIA_CONSTRAINT = """
    ALTER TABLE tasks
        ADD CONSTRAINT tasks_require_validation_criteria
        CHECK (
            task_type = 'epic'
            OR NULLIF(btrim(validation_criteria), '') IS NOT NULL
        ) NOT VALID
"""


def _manager(temp_db: HubDatabase) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


def _create(
    manager: LocalTaskManager,
    project_id: str,
    title: str,
    **kwargs: Any,
) -> Task:
    return manager.create_task(
        project_id,
        title=title,
        validation_criteria="Original validation criteria.",
        **kwargs,
    )


def _make_legacy(
    db: HubDatabase,
    task_id: str,
    *,
    validation_criteria: str | None = None,
) -> None:
    db.execute("ALTER TABLE tasks DROP CONSTRAINT tasks_require_validation_criteria")
    try:
        db.execute(
            "UPDATE tasks SET validation_criteria = %s WHERE id = %s",
            (validation_criteria, task_id),
        )
    finally:
        db.execute(_ADD_VALIDATION_CRITERIA_CONSTRAINT)


def test_legacy_leaf_closes_as_obsolete(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    task = _create(manager, sample_project["id"], "Legacy leaf")
    _make_legacy(temp_db, task.id)

    closed = manager.close_task(task.id, reason="obsolete")

    assert closed.closed_at is not None
    assert closed.closed_reason == "obsolete"
    assert closed.validation_criteria == LEGACY_VALIDATION_CRITERIA


def test_close_preserves_existing_validation_criteria(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    task = _create(manager, sample_project["id"], "Current leaf")

    closed = manager.close_task(task.id, reason="obsolete")

    assert closed.validation_criteria == "Original validation criteria."


def test_forced_cascade_closes_legacy_descendants(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    epic = manager.create_task(
        sample_project["id"],
        title="Legacy epic",
        task_type="epic",
    )
    legacy_child = _create(
        manager,
        sample_project["id"],
        "Legacy child",
        parent_task_id=epic.id,
    )
    _make_legacy(temp_db, legacy_child.id, validation_criteria="   ")

    with temp_db.transaction() as conn:
        _close_task_in_txn(
            conn,
            epic.id,
            db=temp_db,
            reason="merged",
            force=True,
            cascade_descendants=True,
        )

    closed_epic = manager.get_task(epic.id)
    closed_child = manager.get_task(legacy_child.id)
    assert closed_epic is not None
    assert closed_epic.closed_at is not None
    assert closed_epic.validation_criteria is None
    assert closed_child is not None
    assert closed_child.closed_at is not None
    assert closed_child.closed_reason == "merged"
    assert closed_child.validation_criteria == LEGACY_VALIDATION_CRITERIA


def test_legacy_non_epic_ancestor_auto_closes(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    parent = _create(manager, sample_project["id"], "Legacy parent")
    child = _create(
        manager,
        sample_project["id"],
        "Final child",
        parent_task_id=parent.id,
    )
    _make_legacy(temp_db, parent.id)
    closed_ancestors: list[str] = []

    manager.close_task(child.id, reason="obsolete", closed_ancestors=closed_ancestors)

    closed_parent = manager.get_task(parent.id)
    assert closed_ancestors == [parent.id]
    assert closed_parent is not None
    assert closed_parent.closed_at is not None
    assert closed_parent.closed_reason == "obsolete"
    assert closed_parent.validation_criteria == LEGACY_VALIDATION_CRITERIA
