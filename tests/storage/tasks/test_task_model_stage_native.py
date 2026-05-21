"""Stage-native task model regression tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.storage.tasks._models import Task
from gobby.tasks.state_semantics import serialize_task_state

pytestmark = pytest.mark.unit


def test_task_from_row_does_not_require_legacy_status_column() -> None:
    task = Task.from_row(
        {
            "id": "task-1",
            "project_id": "project-1",
            "title": "Stage-native row",
            "priority": 2,
            "task_type": "task",
            "created_at": "2026-05-21T00:00:00+00:00",
            "updated_at": "2026-05-21T00:00:00+00:00",
            "description": None,
            "parent_task_id": None,
            "assignee": None,
            "labels": "[]",
            "closed_reason": None,
        }
    )
    task.stages = (SimpleNamespace(stage_name="development", position=0, state="in_progress"),)

    state = serialize_task_state(task)

    assert not hasattr(task, "status")
    assert "status" not in task.to_dict()
    assert state["current_stage"] == {
        "name": "development",
        "state": "in_progress",
    }
