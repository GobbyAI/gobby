"""Task backup preserves arbitrary evidence independently of runbook steps."""

import json
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskBackupManager
from gobby.tasks.attestations import (
    AttestationRestoreState,
    AttestationSubmission,
    EvidenceReference,
    TaskAttestation,
    list_attestations,
    record_attestation,
    restore_attestations,
)
from tests.fixtures.agent_sessions import AgentSessions as _Seats

pytestmark = pytest.mark.unit


def test_task_backup_round_trips_generic_evidence_and_its_correction(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    author = _Seats(temp_db, sample_project["id"]).add(
        "manuscript-author", [], definition_fields={"task_attestation_writes": ["*"]}
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Manuscript", validation_criteria="Proofread chapter."
    )
    first, _ = record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(
            kind="proofreading",
            result="needs-work",
            summary="Typo found.",
            reference=EvidenceReference(scheme="manuscript", value="chapter:v1"),
        ),
    )
    correction, _ = record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(
            kind="proofreading", result="pass", summary="Typo fixed.", supersedes_id=first.id
        ),
    )
    forged = {**correction.model_dump(mode="json"), "id": str(uuid4()), "sequence": 3}
    temp_db.execute(
        "INSERT INTO task_comments (id, task_id, author, author_type, body) "
        "VALUES (%s, %s, %s, 'session', %s)",
        (forged["id"], task.id, author, json.dumps(forged)),
    )
    path = tmp_path / "tasks.jsonl"
    backup = TaskBackupManager(manager, backup_path=path)
    assert backup.backup(sample_project["id"]) == 1
    exported = json.loads(path.read_text(encoding="utf-8"))
    history = [record.model_dump(mode="json") for record in (first, correction)]
    assert exported["attestations"] == history
    assert "step_records" not in exported.get("review", {})
    temp_db.execute("DELETE FROM tasks WHERE id = %s", (task.id,))
    assert backup.restore(sample_project["id"]) == 1
    assert backup.restore(sample_project["id"]) == 0
    assert list_attestations(temp_db, task.id) == [first, correction]


def test_backup_restore_keeps_divergent_stored_evidence(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    author = _Seats(temp_db, sample_project["id"]).add("divergent-author", ["check"])
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Divergence", validation_criteria="Keep evidence."
    )
    original, _ = record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(kind="check", result="pass", summary="Original."),
    )
    path = tmp_path / "divergent.jsonl"
    backup = TaskBackupManager(manager, backup_path=path)
    backup.backup(sample_project["id"])
    exported = json.loads(path.read_text(encoding="utf-8"))
    exported["attestations"][0]["summary"] = "Different evidence under the same ID."
    exported["updated_at"] = "2099-01-01T00:00:00+00:00"
    exported["closed_at"] = "2099-01-01T00:00:00+00:00"
    exported["closed_reason"] = "done"
    path.write_text(json.dumps(exported) + "\n", encoding="utf-8")
    assert backup.restore(sample_project["id"]) == 0
    assert manager.get_task(task.id).closed_at is None
    assert list_attestations(temp_db, task.id) == [original]


def test_closed_backup_cannot_discard_a_later_local_correction(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    author = _Seats(temp_db, sample_project["id"]).add("later-correction-author", ["check"])
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Later correction", validation_criteria="Keep newer evidence."
    )
    first, _ = record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(
            kind="check", result="pass", summary="Initially approved."
        ),
    )
    path = tmp_path / "older-evidence.jsonl"
    backup = TaskBackupManager(manager, backup_path=path)
    backup.backup(sample_project["id"])
    correction, _ = record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(
            kind="check", result="needs-work", summary="Found an issue.", supersedes_id=first.id
        ),
    )
    exported = json.loads(path.read_text(encoding="utf-8"))
    exported["updated_at"] = "2099-01-01T00:00:00+00:00"
    exported["closed_at"] = "2099-01-01T00:00:00+00:00"
    exported["closed_reason"] = "done"
    path.write_text(json.dumps(exported) + "\n", encoding="utf-8")
    assert backup.restore(sample_project["id"]) == 0
    assert manager.get_task(task.id).closed_at is None
    assert list_attestations(temp_db, task.id) == [first, correction]


@pytest.mark.parametrize(
    "backup_timestamp,expected_count,expected_title",
    [
        ("2099-01-01T00:00:00+00:00", 0, "Newer committed title"),
        ("2101-01-01T00:00:00+00:00", 1, "Older title"),
    ],
)
def test_restore_keeps_current_local_claim_after_bulk_read_before_task_lock(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backup_timestamp: str,
    expected_count: int,
    expected_title: str,
) -> None:
    seats = _Seats(temp_db, sample_project["id"])
    earlier_claimant = seats.add("earlier-claimant")
    current_claimant = seats.add("current-claimant")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Older title", validation_criteria="Keep newer metadata."
    )
    temp_db.execute(
        "UPDATE tasks SET claimed_by_session_id = %s WHERE id = %s", (earlier_claimant, task.id)
    )
    path = tmp_path / "metadata-race.jsonl"
    backup = TaskBackupManager(manager, backup_path=path)
    backup.backup(sample_project["id"])
    exported = json.loads(path.read_text(encoding="utf-8"))
    exported["updated_at"] = backup_timestamp
    exported["claimed_by_session_id"] = None
    exported["state"].pop("owner_session_id", None)
    path.write_text(json.dumps(exported) + "\n", encoding="utf-8")

    def update_metadata() -> None:
        temp_db.execute(
            "UPDATE tasks SET title = %s, updated_at = %s, claimed_by_session_id = %s WHERE id = %s",
            ("Newer committed title", "2100-01-01T00:00:00+00:00", current_claimant, task.id),
        )

    def interleaved_restore(
        conn: Transaction, task_id: str, history: Sequence[TaskAttestation]
    ) -> AttestationRestoreState:
        # The bulk metadata snapshot is already read; the evidence lock is not held yet.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(update_metadata).result(timeout=5)
        return restore_attestations(conn, task_id, history)

    monkeypatch.setattr("gobby.sync.tasks.restore_attestations", interleaved_restore)
    assert backup.restore(sample_project["id"]) == expected_count
    restored = manager.get_task(task.id)
    assert restored.title == expected_title
    assert restored.claimed_by_session_id == current_claimant


def test_review_summary_does_not_restore_any_attestations(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Summary only", validation_criteria="Require evidence."
    )
    path = tmp_path / "summary.jsonl"
    backup = TaskBackupManager(manager, backup_path=path)
    backup.backup(sample_project["id"])
    exported = json.loads(path.read_text(encoding="utf-8"))
    exported["review"]["status"] = "valid"
    exported["updated_at"] = "2099-01-01T00:00:00+00:00"
    path.write_text(json.dumps(exported) + "\n", encoding="utf-8")
    assert backup.restore(sample_project["id"]) == 1
    assert manager.get_task(task.id).review_status == "valid"
    assert list_attestations(temp_db, task.id) == []


@pytest.mark.parametrize(
    "invalid", ["wrong-task", "non-list", "sequence-gap", "uppercase-id", "noncanonical-task"]
)
def test_invalid_evidence_backup_aborts_before_changing_task_rows(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path, invalid: str
) -> None:
    author = _Seats(temp_db, sample_project["id"]).add(
        "backup-validation-author", [], definition_fields={"task_attestation_writes": ["*"]}
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Original title", validation_criteria="Validate backup."
    )
    record, _ = record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(kind="check", result="pass", summary="Original evidence."),
    )
    path = tmp_path / "invalid-tasks.jsonl"
    backup = TaskBackupManager(manager, backup_path=path)
    backup.backup(sample_project["id"])
    exported = json.loads(path.read_text(encoding="utf-8"))
    evidence = record.model_dump(mode="json")
    if invalid == "wrong-task":
        evidence["task_id"] = "another-task"
    elif invalid == "sequence-gap":
        evidence["sequence"] = 2
    elif invalid == "uppercase-id":
        evidence["id"] = "ABCDEFAB-1234-4ABC-ABCD-ABCDEFABCDEF"
    elif invalid == "noncanonical-task":
        exported["id"] = task.id.replace("-", "")
        evidence["task_id"] = exported["id"]
    exported["attestations"] = {"forged": True} if invalid == "non-list" else [evidence]
    exported["title"] = "Corrupted title"
    exported["updated_at"] = "2099-01-01T00:00:00+00:00"
    path.write_text(json.dumps(exported) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        backup.restore(sample_project["id"])
    assert manager.get_task(task.id).title == "Original title"
    assert list_attestations(temp_db, task.id) == [record]
