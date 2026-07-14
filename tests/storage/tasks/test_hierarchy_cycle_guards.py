"""Regression tests for cyclic task-parent data in recursive storage readers."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.build.observability import _subtree_tasks
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import (
    Isolation,
    LocalTaskManager,
    _path_cache,
    cascade_build_state_to_subtree,
)

pytestmark = pytest.mark.unit


def test_path_cache_cycle_fails_before_writing(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    root = manager.create_task(sample_project["id"], "Cycle root")
    child = manager.create_task(sample_project["id"], "Cycle child", parent_task_id=root.id)
    temp_db.execute("UPDATE tasks SET parent_task_id = %s WHERE id = %s", (child.id, root.id))
    temp_db.execute(
        "UPDATE tasks SET path_cache = NULL WHERE id = ANY(%s::uuid[])",
        ([root.id, child.id],),
    )

    assert manager.compute_path_cache(root.id) is None
    with pytest.raises(ValueError, match="Cycle detected"):
        manager.update_descendant_paths(root.id)

    rows = temp_db.fetchall(
        "SELECT path_cache FROM tasks WHERE id = ANY(%s::uuid[])",
        ([root.id, child.id],),
    )
    assert {row["path_cache"] for row in rows} == {None}


def test_descendant_path_update_enforces_depth_cap(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    root = manager.create_task(sample_project["id"], "Depth root")
    child = manager.create_task(sample_project["id"], "Depth child", parent_task_id=root.id)
    grandchild = manager.create_task(
        sample_project["id"], "Depth grandchild", parent_task_id=child.id
    )
    task_ids = [root.id, child.id, grandchild.id]
    temp_db.execute(
        "UPDATE tasks SET path_cache = NULL WHERE id = ANY(%s::uuid[])",
        (task_ids,),
    )
    monkeypatch.setattr(_path_cache, "MAX_TASK_HIERARCHY_DEPTH", 2)

    with pytest.raises(ValueError, match=r"exceeded max depth \(2\)"):
        manager.update_descendant_paths(root.id)

    rows = temp_db.fetchall(
        "SELECT path_cache FROM tasks WHERE id = ANY(%s::uuid[])",
        (task_ids,),
    )
    assert {row["path_cache"] for row in rows} == {None}


def test_ready_and_blocked_readers_terminate_on_blocker_parent_cycle(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    blocked = manager.create_task(sample_project["id"], "Blocked task")
    cycle_root = manager.create_task(sample_project["id"], "Cyclic blocker root")
    blocker = manager.create_task(
        sample_project["id"],
        "Cyclic blocker",
        parent_task_id=cycle_root.id,
    )
    temp_db.execute(
        "UPDATE tasks SET parent_task_id = %s WHERE id = %s", (blocker.id, cycle_root.id)
    )
    TaskDependencyManager(temp_db).add_dependency(blocked.id, blocker.id, "blocks")

    ready_ids = {task.id for task in manager.list_ready_tasks(project_id=sample_project["id"])}
    blocked_ids = {task.id for task in manager.list_blocked_tasks(project_id=sample_project["id"])}

    assert blocked.id not in ready_ids
    assert blocked_ids == {blocked.id}


def test_build_subtree_readers_and_cascade_bound_cycles(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    root = manager.create_task(sample_project["id"], "Build root", task_type="epic")
    child = manager.create_task(sample_project["id"], "Build child", parent_task_id=root.id)
    manager.initialize_task_manifest(root.id, stage_names=["development"])
    temp_db.execute("UPDATE tasks SET parent_task_id = %s WHERE id = %s", (child.id, root.id))

    subtree = _subtree_tasks(manager, manager.get_task(root.id))
    updated = cascade_build_state_to_subtree(
        temp_db,
        root.id,
        Isolation.none,
        unattended=False,
        allow_automation=True,
    )

    assert {task.id for task in subtree} == {root.id, child.id}
    assert updated.updated_count == 2
    assert manager.get_task(root.id).allow_automation is True
    assert manager.get_task(child.id).allow_automation is True
