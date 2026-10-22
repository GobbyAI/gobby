"""Task JSONL import ignores legacy task state keys."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskBackupManager, TaskRestoreError
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit


def _write_jsonl(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


@pytest.mark.parametrize("hold", [None, "needs_human", "deferred"])
def test_restore_persists_supported_fields_from_current_shape(
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    tmp_path: Path,
    hold: str | None,
) -> None:
    project = isolated_checkout_factory(temp_db, "jsonl-shape", root=tmp_path).project
    export_path = tmp_path / ".gobby" / "tasks.jsonl"
    task_id = "11111111-1111-4111-8111-111111111111"
    now = datetime.now(UTC).isoformat()
    _write_jsonl(
        export_path,
        {
            "id": task_id,
            "title": "Imported task",
            "description": "Imported from JSONL",
            "state": {
                "name": hold or "open",
                "hold_state": hold,
                "follow_up_at": now if hold == "deferred" else None,
            },
            "priority": 2,
            "created_at": now,
            "updated_at": now,
            "project_id": project.id,
            "parent_id": None,
            "deps_on": [],
            "commits": [],
            "review": {
                "status": "valid",
                "feedback": "ok",
                "fail_count": 0,
                "epoch": 3,
                "criteria": "criterion",
            },
            "category": "code",
            "seq_num": 987,
        },
    )
    manager = LocalTaskManager(temp_db)

    TaskBackupManager(manager, backup_path=export_path).restore(project_id=project.id)

    task = manager.get_task(task_id)
    assert task is not None
    assert task.task_type == "task"
    assert task.review_status == "valid"
    assert task.review_epoch == 3
    assert task.validation_criteria == "criterion"
    assert task.seq_num == 987
    assert task.hold_state == hold
    assert task.follow_up_at == (datetime.fromisoformat(now) if hold == "deferred" else None)
    assert task.to_dict()["state"]["name"] == (hold or "open")
    TaskBackupManager(manager, backup_path=export_path).backup(project_id=project.id)
    exported = json.loads(export_path.read_text())
    assert exported["state"]["hold_state"] == hold
    assert exported["state"]["follow_up_at"] == (now if hold == "deferred" else None)


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ({"name": "ready"}, "Unknown task state"),
        ({"hold_state": "deferred"}, "Deferred tasks require a follow-up date"),
        ({"hold_state": "invalid"}, "Unknown task hold state"),
        (
            {"hold_state": "deferred", "follow_up_at": "not-a-date"},
            "invalid follow_up_at timestamp",
        ),
        (
            {"closed_at": "2026-10-22T12:00:00Z", "closed_reason": "completed"},
            "Unknown task close reason",
        ),
    ],
)
def test_restore_rejects_invalid_state_before_writing(
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    tmp_path: Path,
    state: dict[str, str],
    message: str,
) -> None:
    project = isolated_checkout_factory(temp_db, "invalid-state", root=tmp_path).project
    export_path = tmp_path / "tasks.jsonl"
    now = datetime.now(UTC).isoformat()
    records = [
        {
            "id": "22222222-2222-4222-8222-222222222222",
            "title": "Valid preceding task",
            "project_id": project.id,
            "created_at": now,
            "updated_at": now,
            "review": {"criteria": "Must remain absent if the next record is invalid."},
            "state": {"name": "open"},
        },
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "title": "Invalid task",
            "project_id": project.id,
            "created_at": now,
            "updated_at": now,
            "review": {"criteria": "Must not be restored."},
            "state": state,
        },
    ]
    export_path.write_text("".join(json.dumps(row) + "\n" for row in records))
    manager = LocalTaskManager(temp_db)
    with pytest.raises(TaskRestoreError, match=f"Task backup line 2: {message}"):
        TaskBackupManager(manager, backup_path=export_path).restore(project_id=project.id)
    assert manager.count_tasks(project_id=project.id) == 0


def test_backup_restore_preserves_label_derived_hold(
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project = isolated_checkout_factory(temp_db, "label-hold", root=tmp_path).project
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=project.id,
        title="Waiting for a decision",
        labels=["awaiting-human-review"],
        validation_criteria="Removing the label releases the hold after restore.",
    )
    export_path = tmp_path / "tasks.jsonl"
    backup = TaskBackupManager(manager, backup_path=export_path)
    assert backup.backup(project_id=project.id) == 1
    exported = json.loads(export_path.read_text())
    assert exported["state"]["name"] == "needs_human"
    assert exported["state"]["hold_state"] is None
    with temp_db.transaction() as conn:
        conn.execute("DELETE FROM tasks WHERE id = %s", (task.id,))

    assert backup.restore(project_id=project.id) == 1
    restored = manager.get_task(task.id)
    assert restored.hold_state is None
    assert restored.to_dict()["state"]["name"] == "needs_human"
    manager.update_task(task.id, labels=[])
    assert manager.get_task(task.id).to_dict()["state"]["name"] == "open"


def test_restore_ignores_top_level_legacy_keys(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db, tmp_path: Path
) -> None:
    project = isolated_checkout_factory(temp_db, "jsonl-legacy", root=tmp_path).project
    export_path = tmp_path / ".gobby" / "tasks.jsonl"
    task_id = "22222222-2222-4222-8222-222222222222"
    now = datetime.now(UTC).isoformat()
    _write_jsonl(
        export_path,
        {
            "id": task_id,
            "title": "Legacy keys",
            "state": {},
            "status": "closed",
            "lifecycle_stage": "done",
            "review_status": "invalid",
            "priority": 2,
            "task_type": "bug",
            "created_at": now,
            "updated_at": now,
            "project_id": project.id,
            "parent_id": None,
            "deps_on": [],
            "commits": [],
            "review": {
                "status": "valid",
                "criteria": "The restored task preserves its nested validation contract.",
            },
            "seq_num": 988,
        },
    )
    manager = LocalTaskManager(temp_db)

    TaskBackupManager(manager, backup_path=export_path).restore(project_id=project.id)

    task = manager.get_task(task_id)
    assert task is not None
    assert task.task_type == "bug"
    assert task.review_status == "valid"
    assert task.validation_criteria == "The restored task preserves its nested validation contract."
    assert task.closed_at is None
