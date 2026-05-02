from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._crud import update_task
from tests.storage.tasks._stage_test_helpers import (
    create_task,
    initialize_manifest,
    set_stage_state,
    spec,
)

pytestmark = pytest.mark.unit


def _task_at_stage(temp_db, sample_project, title: str, state: str):
    task = create_task(
        temp_db,
        sample_project,
        title=title,
        category="test",
        task_type="task",
    )
    initialize_manifest(temp_db, task.id, [spec("development", 0)])
    set_stage_state(temp_db, task.id, "development", state)
    return task


def test_ready_projection_uses_current_stage_state(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    ready = _task_at_stage(temp_db, sample_project, "Ready", "ready")
    in_progress = _task_at_stage(temp_db, sample_project, "In progress", "in_progress")
    needs_review = _task_at_stage(temp_db, sample_project, "Needs review", "needs_review")
    done = _task_at_stage(temp_db, sample_project, "Done", "done")
    no_manifest = create_task(
        temp_db,
        sample_project,
        title="No manifest",
        category="test",
        task_type="task",
    )

    ready_ids = {task.id for task in manager.list_ready_tasks(project_id=sample_project["id"])}

    assert {ready.id, in_progress.id} <= ready_ids
    assert needs_review.id not in ready_ids
    assert done.id not in ready_ids
    assert no_manifest.id not in ready_ids
    assert manager.count_ready_tasks(project_id=sample_project["id"]) == len(ready_ids)


def test_blocked_projection_includes_escalated_and_external_blockers(
    temp_db,
    sample_project,
) -> None:
    manager = LocalTaskManager(temp_db)
    external_blocker = _task_at_stage(temp_db, sample_project, "External blocker", "ready")
    blocked = _task_at_stage(temp_db, sample_project, "Blocked", "ready")
    escalated = _task_at_stage(temp_db, sample_project, "Escalated", "ready")
    parent = create_task(
        temp_db,
        sample_project,
        title="Parent",
        category="planning",
        task_type="epic",
    )
    child = create_task(
        temp_db,
        sample_project,
        title="Child completion blocker",
        category="test",
        task_type="task",
        parent_task_id=parent.id,
    )
    initialize_manifest(temp_db, parent.id, [spec("development", 0)])
    initialize_manifest(temp_db, child.id, [spec("development", 0)])
    now = datetime.now(UTC).isoformat()
    temp_db.execute(
        """
        INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at)
        VALUES (?, ?, 'blocks', ?), (?, ?, 'blocks', ?)
        """,
        (blocked.id, external_blocker.id, now, parent.id, child.id, now),
    )
    update_task(temp_db, escalated.id, escalated_at=now)

    blocked_ids = {task.id for task in manager.list_blocked_tasks(project_id=sample_project["id"])}

    assert {blocked.id, escalated.id} <= blocked_ids
    assert parent.id not in blocked_ids
    assert manager.count_blocked_tasks(project_id=sample_project["id"]) == len(blocked_ids)
