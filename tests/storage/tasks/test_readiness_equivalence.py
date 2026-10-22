from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._updates import update_task

pytestmark = pytest.mark.unit


def test_awaiting_human_review_label_parks_task_until_removed(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        validation_criteria="Task completion is observable.",
        title="Awaiting review",
        category="test",
        task_type="task",
        labels=["llm-reviewed", "awaiting-human-review"],
    )

    ready_ids = {
        candidate.id for candidate in manager.list_ready_tasks(project_id=sample_project["id"])
    }
    blocked_ids = {
        candidate.id for candidate in manager.list_blocked_tasks(project_id=sample_project["id"])
    }
    assert task.id not in ready_ids
    assert task.id in blocked_ids

    update_task(temp_db, task.id, labels=["llm-reviewed", "human-reviewed"])

    ready_ids = {
        candidate.id for candidate in manager.list_ready_tasks(project_id=sample_project["id"])
    }
    assert task.id in ready_ids


def test_blocked_projection_includes_escalated_and_external_blockers(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    external_blocker = manager.create_task(
        sample_project["id"], "External blocker", validation_criteria="External blocker completes."
    )
    blocked = manager.create_task(
        sample_project["id"], "Blocked", validation_criteria="Blocked work completes."
    )
    escalated = manager.create_task(
        sample_project["id"], "Escalated", validation_criteria="Escalated work completes."
    )
    parent = manager.create_task(
        sample_project["id"],
        validation_criteria="Task completion is observable.",
        title="Parent",
        category="planning",
        task_type="epic",
    )
    child = manager.create_task(
        sample_project["id"],
        validation_criteria="Task completion is observable.",
        title="Child completion blocker",
        category="test",
        task_type="task",
        parent_task_id=parent.id,
    )
    now = datetime.now(UTC).isoformat()
    temp_db.execute(
        """
        INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at)
        VALUES (%s, %s, 'blocks', %s), (%s, %s, 'blocks', %s)
        """,
        (blocked.id, external_blocker.id, now, parent.id, child.id, now),
    )
    update_task(temp_db, escalated.id, escalated_at=now)

    blocked_ids = {task.id for task in manager.list_blocked_tasks(project_id=sample_project["id"])}

    assert {blocked.id, escalated.id} <= blocked_ids
    assert parent.id not in blocked_ids
    assert manager.count_blocked_tasks(project_id=sample_project["id"]) == len(blocked_ids)
