"""Task JSONL import ignores legacy task state keys."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager

pytestmark = pytest.mark.unit


def _write_jsonl(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_import_persists_supported_fields_from_current_shape(temp_db, tmp_path: Path) -> None:
    project = LocalProjectManager(temp_db).create("jsonl-shape", repo_path=str(tmp_path))
    export_path = tmp_path / ".gobby" / "tasks.jsonl"
    now = datetime.now(UTC).isoformat()
    _write_jsonl(
        export_path,
        {
            "id": "task-jsonl-shape-1",
            "title": "Imported task",
            "description": "Imported from JSONL",
            "state": {},
            "priority": 2,
            "created_at": now,
            "updated_at": now,
            "project_id": project.id,
            "parent_id": None,
            "deps_on": [],
            "commits": [],
            "validation": {
                "state": "valid",
                "feedback": "ok",
                "fail_count": 0,
                "criteria": "criterion",
            },
            "category": "code",
            "seq_num": 987,
        },
    )
    manager = LocalTaskManager(temp_db)

    TaskSyncManager(manager).import_from_jsonl(project_id=project.id)

    task = manager.get_task("task-jsonl-shape-1")
    assert task is not None
    assert task.task_type == "task"
    assert task.validation_status == "valid"
    assert task.validation_criteria == "criterion"
    assert task.seq_num == 987


def test_import_ignores_top_level_legacy_keys(temp_db, tmp_path: Path) -> None:
    project = LocalProjectManager(temp_db).create("jsonl-legacy", repo_path=str(tmp_path))
    export_path = tmp_path / ".gobby" / "tasks.jsonl"
    now = datetime.now(UTC).isoformat()
    _write_jsonl(
        export_path,
        {
            "id": "task-jsonl-shape-2",
            "title": "Legacy keys",
            "state": {},
            "status": "closed",
            "lifecycle_stage": "done",
            "validation_status": "invalid",
            "priority": 2,
            "task_type": "bug",
            "created_at": now,
            "updated_at": now,
            "project_id": project.id,
            "parent_id": None,
            "deps_on": [],
            "commits": [],
            "validation": {"state": "valid"},
            "seq_num": 988,
        },
    )
    manager = LocalTaskManager(temp_db)

    TaskSyncManager(manager).import_from_jsonl(project_id=project.id)

    task = manager.get_task("task-jsonl-shape-2")
    assert task is not None
    assert task.task_type == "bug"
    assert task.validation_status == "valid"
    assert task.closed_at is None
