"""One-time conversion of historical task evidence; never a legacy read path."""

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.tasks.attestations import (
    AUTHOR_TYPE,
    AttestationError,
    TaskAttestation,
    validate_attestation_history,
)

LEGACY_AUTHOR_TYPE = "task_step"


class SourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    task_id: str
    author: str
    author_type: str
    body: str


class ConversionBackup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[1] = 1
    project_id: str
    source_rows: tuple[SourceEvidence, ...] = Field(default_factory=tuple)


def save_conversion_backup(backup: ConversionBackup, path: Path) -> None:
    """Persist a new backup before any conversion writes are allowed."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(backup.model_dump_json())
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_conversion_backup(path: Path) -> ConversionBackup:
    """Read the exact backup needed by conversion and restore."""
    return ConversionBackup.model_validate_json(path.read_text(encoding="utf-8"))


def _rows(conn: Transaction, project_id: str) -> tuple[SourceEvidence, ...]:
    rows = conn.execute(
        "SELECT c.id, c.task_id, c.author, c.author_type, c.body FROM task_comments c "
        "JOIN tasks t ON t.id = c.task_id WHERE t.project_id = %s "
        "AND c.author_type IN (%s, %s) ORDER BY c.id",
        (project_id, LEGACY_AUTHOR_TYPE, AUTHOR_TYPE),
    ).fetchall()
    return tuple(SourceEvidence.model_validate(dict(row)) for row in rows)


def _converted(source: SourceEvidence) -> SourceEvidence:
    """Derive new records from source bytes, never from editable backup assertions."""
    if source.author_type != LEGACY_AUTHOR_TYPE:
        raise AttestationError("conversion source must contain only historical step records")
    try:
        data: Any = json.loads(source.body)
        if not isinstance(data, dict):
            raise AttestationError("historical record must be an object")
        if (data.get("id"), data.get("task_id"), data.get("session_id")) != (
            source.id,
            source.task_id,
            source.author,
        ):
            raise AttestationError("historical identity disagrees with stored provenance")
        sha = data.get("commit_sha")
        reference = {"scheme": "git", "value": sha} if sha else None
        record = TaskAttestation.model_validate(
            {
                "id": source.id,
                "task_id": source.task_id,
                "session_id": source.author,
                "sequence": data["sequence"],
                "agent_definition": data["agent_definition"],
                "agent_definition_hash": data["agent_definition_hash"],
                "agent_capabilities": data.get("agent_capabilities", []),
                "created_at": data["created_at"],
                "kind": data["step"],
                "result": data["status"],
                "summary": data["feedback"],
                "reference": reference,
                "evidence": {"source_record": data},
            }
        )
    except (KeyError, ValueError, ValidationError) as exc:
        raise AttestationError(f"invalid historical evidence {source.id}: {exc}") from exc
    return source.model_copy(update={"author_type": AUTHOR_TYPE, "body": record.model_dump_json()})


def prepare_conversion(db: HubDatabase, *, project_id: str) -> ConversionBackup:
    with db.transaction() as conn:
        rows = _rows(conn, project_id)
        _converted_rows(rows)
        return ConversionBackup(project_id=project_id, source_rows=rows)


def _converted_rows(originals: tuple[SourceEvidence, ...]) -> tuple[SourceEvidence, ...]:
    converted = tuple(_converted(source) for source in originals)
    histories: dict[str, list[TaskAttestation]] = {}
    for row in converted:
        histories.setdefault(row.task_id, []).append(TaskAttestation.model_validate_json(row.body))
    for task_id, history in histories.items():
        try:
            validate_attestation_history(
                task_id, sorted(history, key=lambda record: record.sequence)
            )
        except AttestationError as exc:
            raise AttestationError(f"invalid converted history for task {task_id}: {exc}") from exc
    return converted


def _replace(db: HubDatabase, backup: ConversionBackup, *, restore: bool) -> int:
    originals = backup.source_rows
    converted = _converted_rows(originals)
    expected, desired = (converted, originals) if restore else (originals, converted)
    with db.transaction() as conn:
        # Evidence and closes serialize on these same task locks. The operational
        # writer pause also prevents new tasks from appearing during the cutover.
        conn.execute(
            "SELECT id FROM tasks WHERE project_id = %s ORDER BY id FOR UPDATE",
            (backup.project_id,),
        ).fetchall()
        current = _rows(conn, backup.project_id)
        if current == desired:
            return 0
        if current != expected:
            raise AttestationError("evidence changed since the conversion backup; no rows written")
        for source in desired:
            conn.execute(
                "UPDATE task_comments SET author_type = %s, body = %s WHERE id = %s AND task_id = %s",
                (source.author_type, source.body, source.id, source.task_id),
            )
    return len(desired)


def apply_conversion(db: HubDatabase, backup: ConversionBackup) -> int:
    return _replace(db, backup, restore=False)


def restore_conversion(db: HubDatabase, backup: ConversionBackup) -> int:
    return _replace(db, backup, restore=True)
