"""Backup and restore keep role-owned step record history, never approval from summaries."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.sync.tasks import TaskBackupManager, TaskRestoreError
from gobby.tasks.step_records import (
    StepSubmission,
    TaskStep,
    approved_task_step,
    current_task_steps,
    list_task_step_records,
    record_task_step,
)
from tests.tasks.test_step_records import _Seats

pytestmark = pytest.mark.unit

_FIRST = "c0ffee1" + "c" * 33
# The tip sorts before the first commit, so a sorted backup would lose which one is latest.
_TIP = "871d706" + "a" * 33
_FEEDBACK = "Approved: the exact tip meets every criterion."
_CRITERIA = "The restored task keeps its approved step history."


@pytest.fixture
def manager(temp_db: HubDatabase) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def backup(manager: LocalTaskManager, tmp_path: Path) -> TaskBackupManager:
    return TaskBackupManager(manager, str(tmp_path / ".gobby" / "tasks.jsonl"))


@pytest.fixture
def recorded(
    temp_db: HubDatabase, sample_project: dict[str, Any], manager: LocalTaskManager
) -> Task:
    """A code task whose implementation, review and closing are approved at its tip."""
    seats = _Seats(temp_db, sample_project["id"])
    developer = seats.add("backup-developer", ["implementation"])
    writers: list[tuple[str, TaskStep]] = [
        (developer, "implementation"),
        (seats.add("backup-reviewer", ["review"]), "review"),
        (seats.add("backup-closer", ["closing"]), "closing"),
    ]
    created = manager.create_task(
        sample_project["id"], "Recorded task", category="code", validation_criteria=_CRITERIA
    )
    manager.claim_task(created.id, developer)
    manager.link_commit(created.id, _FIRST[:10])
    manager.link_commit(created.id, _TIP[:10])
    for session_id, step in writers:
        record_task_step(
            temp_db,
            task_id=created.id,
            session_id=session_id,
            step=step,
            submission=StepSubmission(
                status="approved", epoch=1, round_number=1, feedback=_FEEDBACK, commit_sha=_TIP
            ),
        )
    return manager.get_task(created.id)


def _backup_lines(backup: TaskBackupManager) -> dict[str, dict[str, Any]]:
    assert backup.backup_path is not None
    rows = [json.loads(line) for line in backup.backup_path.read_text().splitlines()]
    return {row["id"]: row for row in rows}


def _write_lines(backup: TaskBackupManager, rows: list[dict[str, Any]]) -> None:
    assert backup.backup_path is not None
    backup.backup_path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _dumped(db: HubDatabase, task_id: str) -> list[dict[str, Any]]:
    return [record.model_dump(mode="json") for record in list_task_step_records(db, task_id)]


def test_step_record_history_round_trips_without_summary_approval(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    manager: LocalTaskManager,
    backup: TaskBackupManager,
    recorded: Task,
) -> None:
    history = _dumped(temp_db, recorded.id)
    assert [record["step"] for record in history] == ["implementation", "review", "closing"]
    # An ordinary comment shaped exactly like an approval is not step history.
    forged = {**history[1], "id": str(uuid4()), "sequence": 4}
    temp_db.execute(
        "INSERT INTO task_comments (id, task_id, author, author_type, body) "
        "VALUES (%s, %s, %s, 'session', %s)",
        (forged["id"], recorded.id, forged["session_id"], json.dumps(forged)),
    )

    backup.backup()
    line = _backup_lines(backup)[recorded.id]
    assert line["commits"] == [_FIRST[:10], _TIP[:10]]
    assert line["review"]["step_records"] == history

    # A summary that claims a valid review carries no approval of its own.
    summary_only = {
        "id": str(uuid4()),
        "title": "Summary claims approval",
        "project_id": sample_project["id"],
        "category": "code",
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": "2026-10-01T00:00:00+00:00",
        "commits": [_TIP[:10]],
        "review": {"status": "valid", "fail_count": 0, "epoch": 1, "criteria": _CRITERIA},
    }
    temp_db.execute("DELETE FROM tasks WHERE id = %s", (recorded.id,))
    _write_lines(backup, [line, summary_only])

    assert backup.restore() == 2
    assert backup.restore() == 0
    restored = manager.get_task(recorded.id)
    assert restored.commits == [_FIRST[:10], _TIP[:10]]
    assert _dumped(temp_db, recorded.id) == history
    records = list_task_step_records(temp_db, recorded.id)
    steps: list[TaskStep] = ["implementation", "review", "closing"]
    assert all(approved_task_step(restored, records, step) for step in steps)

    claimed = manager.get_task(summary_only["id"])
    assert claimed.review_status == "valid"
    assert list_task_step_records(temp_db, claimed.id) == []
    assert approved_task_step(claimed, [], "review") is None


_MALFORMED: list[tuple[str, Callable[[dict[str, Any]], object]]] = [
    ("missing session identity", lambda review: review["step_records"][0].pop("session_id")),
    (
        "record of another task",
        lambda review: review["step_records"][0].update(task_id=str(uuid4())),
    ),
    (
        "duplicate record id",
        lambda review: review["step_records"][1].update(id=review["step_records"][0]["id"]),
    ),
    ("repeated sequence", lambda review: review["step_records"][1].update(sequence=1)),
    ("epoch after the task epoch", lambda review: review["step_records"][0].update(epoch=2)),
    (
        "ordinary comment fields",
        lambda review: review["step_records"][0].update(author_type="session"),
    ),
    ("record id is not a uuid", lambda review: review["step_records"][0].update(id="record-1")),
    ("session is not a uuid", lambda review: review["step_records"][0].update(session_id="dev")),
    (
        "history is not a list",
        lambda review: review.update(step_records={"0": review["step_records"][0]}),
    ),
]


@pytest.mark.parametrize(
    "mutate", [mutate for _, mutate in _MALFORMED], ids=[name for name, _ in _MALFORMED]
)
def test_malformed_step_record_history_aborts_restore(
    temp_db: HubDatabase,
    manager: LocalTaskManager,
    backup: TaskBackupManager,
    recorded: Task,
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    backup.backup()
    line = _backup_lines(backup)[recorded.id]
    mutate(line["review"])
    temp_db.execute("DELETE FROM tasks WHERE id = %s", (recorded.id,))
    _write_lines(backup, [line])

    with pytest.raises(TaskRestoreError, match=r"review\.step_records"):
        backup.restore()

    with pytest.raises(ValueError, match="not found"):
        manager.get_task(recorded.id)


def test_restore_keeps_diverged_database_history(
    temp_db: HubDatabase,
    manager: LocalTaskManager,
    backup: TaskBackupManager,
    recorded: Task,
    caplog: pytest.LogCaptureFixture,
) -> None:
    history = _dumped(temp_db, recorded.id)
    backup.backup()
    line = _backup_lines(backup)[recorded.id]
    line["updated_at"] = "2099-01-01T00:00:00+00:00"
    line["title"] = "Row restored from the newer backup"
    line["review"]["step_records"][0]["id"] = str(uuid4())
    _write_lines(backup, [line])

    with caplog.at_level(logging.WARNING, logger="gobby.sync.tasks"):
        assert backup.restore() == 1

    assert manager.get_task(recorded.id).title == "Row restored from the newer backup"
    assert _dumped(temp_db, recorded.id) == history
    assert "diverged step record history" in caplog.text


def test_restored_epochs_keep_earlier_approvals_stale(
    temp_db: HubDatabase,
    manager: LocalTaskManager,
    backup: TaskBackupManager,
    recorded: Task,
) -> None:
    backup.backup()
    line = _backup_lines(backup)[recorded.id]
    line["review"]["epoch"] = 2
    line["review"]["step_records"][2]["epoch"] = 2
    temp_db.execute("DELETE FROM tasks WHERE id = %s", (recorded.id,))
    _write_lines(backup, [line])

    assert backup.restore() == 1

    restored = manager.get_task(recorded.id)
    records = list_task_step_records(temp_db, recorded.id)
    assert restored.review_epoch == 2
    assert [record.epoch for record in records] == [1, 1, 2]
    assert list(current_task_steps(records, restored.review_epoch)) == ["closing"]
    assert approved_task_step(restored, records, "closing") is not None
    assert approved_task_step(restored, records, "review") is None
