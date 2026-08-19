from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task

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


def _manager(temp_db: HubDatabase, tmp_path: Path) -> tuple[LocalTaskManager, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = LocalProjectManager(temp_db).create(
        name="ancestor-close",
        repo_path=str(repo),
    )
    return LocalTaskManager(temp_db), project.id


def _create(
    manager: LocalTaskManager,
    project_id: str,
    title: str,
    **kwargs: Any,
) -> Task:
    return manager.create_task(
        project_id,
        title=title,
        validation_criteria="Observable completion.",
        **kwargs,
    )


def _open(manager: LocalTaskManager, task_id: str) -> bool:
    task = manager.get_task(task_id)
    assert task is not None
    return task.closed_at is None
