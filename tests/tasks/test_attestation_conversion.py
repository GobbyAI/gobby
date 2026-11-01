"""One-time conversion preserves source bytes and refuses unsafe cutovers."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.attestation_conversion import (
    ConversionBackup,
    SourceEvidence,
    apply_conversion,
    load_conversion_backup,
    prepare_conversion,
    restore_conversion,
    save_conversion_backup,
)
from gobby.tasks.attestations import (
    AttestationError,
    AttestationSubmission,
    list_attestations,
    record_attestation,
)
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit


def _seed_legacy(
    db: HubDatabase, project_id: str, author: str, feedback: str = "Implemented and checked."
) -> tuple[Task, SourceEvidence]:
    """Historical bytes are fixtures, independent of any retired writer API."""
    manager = LocalTaskManager(db)
    task = manager.create_task(
        project_id, "Historical evidence", validation_criteria="Preserve evidence."
    )
    sha = "a" * 40
    manager.link_commit(task.id, sha)
    identity = str(uuid4())
    body = json.dumps(
        {
            "id": identity,
            "task_id": task.id,
            "session_id": author,
            "agent_definition": "historical-author",
            "agent_definition_hash": "f" * 64,
            "agent_capabilities": ["historical-writer"],
            "created_at": "2026-10-01T00:00:00+00:00",
            "sequence": 1,
            "step": "implementation",
            "status": "approved",
            "feedback": feedback,
            "commit_sha": sha,
            "epoch": 1,
            "round_number": 1,
            "legacy_metadata": {"preserve": [1, True, "all supplemental data"]},
        }
    )
    source = SourceEvidence(
        id=identity, task_id=task.id, author=author, author_type="task_step", body=body
    )
    db.execute(
        "INSERT INTO task_comments (id, task_id, author, author_type, body) VALUES (%s, %s, %s, %s, %s)",
        (source.id, source.task_id, source.author, source.author_type, source.body),
    )
    return task, source


def test_conversion_backup_is_durable_private_and_never_overwritten(tmp_path: Path) -> None:
    backup = ConversionBackup(project_id="one-project")
    path = tmp_path / "conversion-backup.json"
    save_conversion_backup(backup, path)
    assert load_conversion_backup(path) == backup
    assert path.stat().st_mode & 0o777 == 0o600
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        save_conversion_backup(ConversionBackup(project_id="another-project"), path)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "feedback", ["Implemented and checked.", "Legacy detail. " * 80000], ids=["normal", "large"]
)
def test_conversion_preserves_history_and_can_restore_exact_source(
    temp_db: HubDatabase, sample_project: dict[str, Any], feedback: str, tmp_path: Path
) -> None:
    project_id = sample_project["id"]
    author = AgentSessions(temp_db, project_id).add("conversion-author")
    task, source = _seed_legacy(temp_db, project_id, author, feedback)
    before = temp_db.fetchall("SELECT * FROM task_comments WHERE task_id = %s", (task.id,))
    path = tmp_path / "conversion.json"
    save_conversion_backup(prepare_conversion(temp_db, project_id=project_id), path)
    backup = load_conversion_backup(path)
    assert apply_conversion(temp_db, backup) == 1
    records = list_attestations(temp_db, task.id)
    assert len(records) == 1
    record = records[0]
    original = json.loads(source.body)
    assert (record.id, record.session_id, record.agent_definition_hash, record.created_at) == (
        source.id,
        author,
        original["agent_definition_hash"],
        datetime.fromisoformat(original["created_at"]),
    )
    assert record.kind == "implementation"
    assert record.result == "approved"
    assert record.summary == feedback
    assert record.reference is not None
    assert (record.reference.scheme, record.reference.value) == ("git", "a" * 40)
    assert record.evidence["source_record"] == original
    assert apply_conversion(temp_db, backup) == 0
    assert restore_conversion(temp_db, load_conversion_backup(path)) == 1
    assert temp_db.fetchall("SELECT * FROM task_comments WHERE task_id = %s", (task.id,)) == before
    assert list_attestations(temp_db, task.id) == []


def test_stale_conversion_backup_refuses_atomically(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    author = AgentSessions(temp_db, project_id).add("stale-conversion-author")
    task, source = _seed_legacy(temp_db, project_id, author)
    backup = prepare_conversion(temp_db, project_id=project_id)
    extra = {
        **json.loads(source.body),
        "id": str(uuid4()),
        "sequence": 2,
        "feedback": "Another result.",
    }
    temp_db.execute(
        "INSERT INTO task_comments (id, task_id, author, author_type, body) VALUES (%s, %s, %s, 'task_step', %s)",
        (extra["id"], task.id, author, json.dumps(extra)),
    )
    with pytest.raises(AttestationError, match="changed"):
        apply_conversion(temp_db, backup)
    assert list_attestations(temp_db, task.id) == []


def test_restore_refuses_to_erase_evidence_written_after_conversion(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    author = AgentSessions(temp_db, project_id).add("post-conversion-author", ["*"])
    task, _ = _seed_legacy(temp_db, project_id, author)
    backup = prepare_conversion(temp_db, project_id=project_id)
    apply_conversion(temp_db, backup)
    record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(kind="follow-up", result="pass", summary="New evidence."),
    )
    before = temp_db.fetchall(
        "SELECT * FROM task_comments WHERE task_id = %s ORDER BY id", (task.id,)
    )
    with pytest.raises(AttestationError, match="changed"):
        restore_conversion(temp_db, backup)
    assert (
        temp_db.fetchall("SELECT * FROM task_comments WHERE task_id = %s ORDER BY id", (task.id,))
        == before
    )
    assert len(list_attestations(temp_db, task.id)) == 2


@pytest.mark.parametrize("invalid", ["sequence-gap", "noncanonical-author", "nonfinite-metadata"])
def test_conversion_validates_complete_target_history_before_any_write(
    temp_db: HubDatabase, sample_project: dict[str, Any], invalid: str
) -> None:
    project_id = sample_project["id"]
    author = AgentSessions(temp_db, project_id).add("invalid-conversion-author")
    valid_task, valid_source = _seed_legacy(temp_db, project_id, author)
    task, source = _seed_legacy(temp_db, project_id, author)
    body = json.loads(source.body)
    if invalid == "sequence-gap":
        body["sequence"] = 2
    elif invalid == "noncanonical-author":
        body["session_id"] = "ABCDEFAB-1234-4ABC-ABCD-ABCDEFABCDEF"
        source = source.model_copy(update={"author": body["session_id"]})
    else:
        body["legacy_metadata"] = {"reading": float("nan")}
    source = source.model_copy(update={"body": json.dumps(body)})
    temp_db.execute(
        "UPDATE task_comments SET author = %s, body = %s WHERE id = %s",
        (source.author, source.body, source.id),
    )
    before = temp_db.fetchall("SELECT * FROM task_comments ORDER BY id")
    with pytest.raises(AttestationError):
        prepare_conversion(temp_db, project_id=project_id)
    backup = ConversionBackup(project_id=project_id, source_rows=(valid_source, source))
    with pytest.raises(AttestationError):
        apply_conversion(temp_db, backup)
    assert temp_db.fetchall("SELECT * FROM task_comments ORDER BY id") == before
    assert list_attestations(temp_db, task.id) == []
    assert list_attestations(temp_db, valid_task.id) == []
