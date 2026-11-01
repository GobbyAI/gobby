"""Isolated evidence cutover, recovery, and JSONL restore proof."""

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import gobby.cli.attestation_cutover as cutover
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskBackupError, TaskBackupManager
from gobby.tasks.attestations import list_attestations
from tests.fixtures.agent_sessions import AgentSessions
from tests.tasks.test_attestation_conversion import _seed_legacy

pytestmark = pytest.mark.unit


def test_cutover_preserves_source_and_proves_converted_backup_restore(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def database(*, apply_migrations: bool = True) -> HubDatabase:
        assert not apply_migrations
        return temp_db

    monkeypatch.setattr(cutover, "require_cli_database", database)
    project_id = sample_project["id"]
    author = AgentSessions(temp_db, project_id).add("cutover-author")
    task, source = _seed_legacy(temp_db, project_id, author, "Original evidence")
    original_rows = temp_db.fetchall("SELECT * FROM task_comments WHERE task_id = %s", (task.id,))
    backup = tmp_path / "conversion.json"
    task_backup = tmp_path / "tasks.jsonl"
    legacy_jsonl = b'{"legacy": "opaque bytes retained without interpretation"}\r\n'
    task_backup.write_bytes(legacy_jsonl)
    runner = CliRunner()
    options = [
        "--project-id",
        project_id,
        "--backup",
        str(backup),
        "--task-backup",
        str(task_backup),
    ]

    prepared = runner.invoke(cutover.main, ["prepare", *options])
    assert prepared.exit_code == 0, prepared.output
    assert backup.exists()
    assert backup.with_name(backup.name + ".tasks.jsonl").read_bytes() == legacy_jsonl
    assert backup.stat().st_mode & 0o777 == 0o600
    assert (
        temp_db.fetchall("SELECT * FROM task_comments WHERE task_id = %s", (task.id,))
        == original_rows
    )

    saved_backup = backup.read_bytes()
    repeated = runner.invoke(cutover.main, ["prepare", *options])
    assert repeated.exit_code == 1
    assert "already exist" in repeated.output
    assert backup.read_bytes() == saved_backup

    def fail_backup(self: TaskBackupManager, project_id: str | None = None) -> int:
        raise TaskBackupError("simulated disk failure")

    with monkeypatch.context() as failure:
        failure.setattr(TaskBackupManager, "backup", fail_backup)
        interrupted = runner.invoke(cutover.main, ["apply", *options])
    assert interrupted.exit_code == 1
    assert "simulated disk failure" in interrupted.output
    assert len(list_attestations(temp_db, task.id)) == 1
    assert task_backup.read_bytes() == legacy_jsonl

    applied = runner.invoke(cutover.main, ["apply", *options])
    assert applied.exit_code == 0, applied.output
    records = list_attestations(temp_db, task.id)
    assert len(records) == 1
    assert records[0].id == source.id
    assert records[0].summary == "Original evidence"
    exported = json.loads(task_backup.read_text())
    assert exported["attestations"][0]["id"] == source.id

    restored = runner.invoke(cutover.main, ["restore", *options])
    assert restored.exit_code == 0, restored.output
    assert task_backup.read_bytes() == legacy_jsonl
    assert (
        temp_db.fetchall("SELECT * FROM task_comments WHERE task_id = %s", (task.id,))
        == original_rows
    )

    reapplied = runner.invoke(cutover.main, ["apply", *options])
    assert reapplied.exit_code == 0, reapplied.output
    manager = LocalTaskManager(temp_db)
    manager.delete_task(task.id)
    assert TaskBackupManager(manager, task_backup).restore(project_id) == 1
    assert list_attestations(temp_db, task.id) == records


@pytest.mark.parametrize("action", ["apply", "restore"])
@pytest.mark.parametrize("mismatch", ["destination", "archive"])
def test_cutover_refuses_changed_artifact_binding_before_any_mutation(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    mismatch: str,
) -> None:
    monkeypatch.setattr(cutover, "require_cli_database", lambda **_kwargs: temp_db)
    project_id = sample_project["id"]
    author = AgentSessions(temp_db, project_id).add("cutover-author")
    task, _ = _seed_legacy(temp_db, project_id, author, "Original evidence")
    backup = tmp_path / "conversion.json"
    original = tmp_path / "original.jsonl"
    other = tmp_path / "other.jsonl"
    original.write_bytes(b"original backup\r\n")
    other.write_bytes(b"unrelated backup\n")
    options = ["--project-id", project_id, "--backup", str(backup), "--task-backup", str(original)]
    runner = CliRunner()
    prepared = runner.invoke(cutover.main, ["prepare", *options])
    assert prepared.exit_code == 0, prepared.output
    if action == "restore":
        applied = runner.invoke(cutover.main, ["apply", *options])
        assert applied.exit_code == 0, applied.output
    before = temp_db.fetchall("SELECT * FROM task_comments WHERE task_id = %s", (task.id,))
    original_bytes = original.read_bytes()
    if mismatch == "destination":
        options[-1] = str(other)
    else:
        backup.with_name(backup.name + ".tasks.jsonl").write_bytes(b"wrong archive\n")
    refused = runner.invoke(cutover.main, [action, *options])
    assert refused.exit_code == 1, refused.output
    assert mismatch in refused.output.lower()
    assert temp_db.fetchall("SELECT * FROM task_comments WHERE task_id = %s", (task.id,)) == before
    assert original.read_bytes() == original_bytes
    assert other.read_bytes() == b"unrelated backup\n"


def test_cutover_uses_the_bound_canonical_destination_for_symlink_io(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cutover, "require_cli_database", lambda **_kwargs: temp_db)
    project_id = sample_project["id"]
    author = AgentSessions(temp_db, project_id).add("cutover-author")
    _seed_legacy(temp_db, project_id, author, "Original evidence")
    backup = tmp_path / "conversion.json"
    target = tmp_path / "tasks.jsonl"
    alias = tmp_path / "alias.jsonl"
    target.write_bytes(b"original backup\r\n")
    alias.symlink_to(target)
    options = ["--project-id", project_id, "--backup", str(backup), "--task-backup", str(alias)]
    runner = CliRunner()
    for action in ("prepare", "apply", "restore"):
        result = runner.invoke(cutover.main, [action, *options])
        assert result.exit_code == 0, result.output
        assert alias.is_symlink()
    assert target.read_bytes() == b"original backup\r\n"
