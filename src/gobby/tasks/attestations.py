"""Authenticated, append-only task evidence without domain workflow requirements."""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import Task
from gobby.tasks.attestation_authorship import attestation_authorship_refusal
from gobby.tasks.attestation_shapes import attestation_shape_refusal
from gobby.tasks.claim_flow import advance_claim_flow
from gobby.tasks.lifecycle_access import task_scope_refusal
from gobby.utils.datetime import utc_now
from gobby.workflows.agent_resolver import session_agent_definition

AUTHOR_TYPE = "task_attestation"
type AttestationRestoreState = Literal["diverged", "ahead", "unchanged", "extended"]


class AttestationError(ValueError):
    """Evidence violates an integrity or writer-authority constraint."""


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scheme: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=8192)


class AttestationContent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=128)
    result: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1)
    evidence: dict[str, JsonValue] = Field(default_factory=dict)
    reference: EvidenceReference | None = None
    supersedes_id: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_finite_evidence(self) -> Self:
        try:
            json.dumps(self.evidence, allow_nan=False)
        except ValueError as exc:
            raise ValueError("evidence numbers must be finite") from exc
        return self


class AttestationSubmission(AttestationContent):
    """Bound new requests without imposing new size limits on historical evidence."""

    summary: str = Field(min_length=1, max_length=65536)

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if not all(value.strip() for value in (self.kind, self.result, self.summary)):
            raise ValueError("kind, result and summary require non-whitespace content")
        payload = json.dumps(self.evidence, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(payload) > 1048576:
            raise ValueError("evidence must fit within 1048576 bytes")
        return self


class TaskAttestation(AttestationContent):
    id: str
    task_id: str
    sequence: int = Field(gt=0, strict=True)
    session_id: str
    agent_definition: str
    agent_definition_hash: str
    agent_capabilities: tuple[str, ...]
    created_at: datetime


def list_attestations(db: HubDatabase, task_id: str) -> list[TaskAttestation]:
    with db.transaction() as conn:
        return _records(conn, task_id)


def attestations_by_task(db: HubDatabase) -> dict[str, list[TaskAttestation]]:
    """Read authenticated evidence histories for task backup."""
    rows = db.fetchall(
        "SELECT id, task_id, author, body FROM task_comments WHERE author_type = %s",
        (AUTHOR_TYPE,),
    )
    history: dict[str, list[TaskAttestation]] = {}
    for record in sorted(map(_stored_record, rows), key=lambda record: record.sequence):
        history.setdefault(record.task_id, []).append(record)
    for task_id, records in history.items():
        validate_attestation_history(task_id, records)
    return history


def restore_attestations(
    conn: Transaction, task_id: str, history: Sequence[TaskAttestation]
) -> AttestationRestoreState:
    """Append trusted backup history without replacing existing evidence."""
    if (
        conn.execute("SELECT id FROM tasks WHERE id = %s FOR NO KEY UPDATE", (task_id,)).fetchone()
        is None
    ):
        raise AttestationError("attestation restore task does not exist")
    validate_attestation_history(task_id, history)
    stored = _records(conn, task_id)
    shared = min(len(stored), len(history))
    if any(not _same_content(stored[index], history[index]) for index in range(shared)):
        return "diverged"
    if len(stored) > len(history):
        return "ahead"
    for record in history[len(stored) :]:
        conn.execute(
            "INSERT INTO task_comments "
            "(id, task_id, author, author_type, body, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                record.id,
                task_id,
                record.session_id,
                AUTHOR_TYPE,
                record.model_dump_json(),
                record.created_at,
                record.created_at,
            ),
        )
    return "extended" if len(history) > len(stored) else "unchanged"


def validate_attestation_history(task_id: str, history: Sequence[TaskAttestation]) -> None:
    """Validate the append order and correction graph before reading or restoring evidence."""
    if not _is_canonical_uuid(task_id):
        raise AttestationError("attestation task identity must be a canonical UUID")
    seen: dict[str, TaskAttestation] = {}
    superseded: set[str] = set()
    keys: set[tuple[str, str]] = set()
    for sequence, record in enumerate(history, start=1):
        if not all(
            _is_canonical_uuid(value) for value in (record.id, record.task_id, record.session_id)
        ):
            raise AttestationError("attestation provenance identities must be canonical UUIDs")
        if record.task_id != task_id or record.sequence != sequence or record.id in seen:
            raise AttestationError("attestation history identity or sequence is invalid")
        if record.idempotency_key is not None:
            key = (record.session_id, record.idempotency_key)
            if key in keys:
                raise AttestationError("attestation history repeats an idempotency key")
            keys.add(key)
        if record.supersedes_id is not None:
            original = seen.get(record.supersedes_id)
            if (
                original is None
                or original.id in superseded
                or (original.kind, original.session_id) != (record.kind, record.session_id)
            ):
                raise AttestationError("attestation history has an invalid correction")
            superseded.add(original.id)
        seen[record.id] = record


def _is_canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _same_content(left: AttestationContent, right: AttestationContent) -> bool:
    # Python/model equality treats True == 1. Evidence equality preserves JSON types
    # while ignoring object-key order, including nested metadata and source records.
    return json.dumps(left.model_dump(mode="json"), sort_keys=True, allow_nan=False) == json.dumps(
        right.model_dump(mode="json"), sort_keys=True, allow_nan=False
    )


def _stored_record(row: Mapping[str, Any]) -> TaskAttestation:
    record = TaskAttestation.model_validate_json(row["body"])
    if (record.id, record.task_id, record.session_id) != (
        str(row["id"]),
        str(row["task_id"]),
        row["author"],
    ):
        raise AttestationError("attestation identity disagrees with stored provenance")
    return record


def _records(conn: Transaction, task_id: str) -> list[TaskAttestation]:
    rows = conn.execute(
        "SELECT id, task_id, author, body FROM task_comments "
        "WHERE task_id = %s AND author_type = %s",
        (task_id, AUTHOR_TYPE),
    ).fetchall()
    records = sorted(map(_stored_record, rows), key=lambda record: record.sequence)
    validate_attestation_history(task_id, records)
    return records


def record_attestation(
    db: HubDatabase, *, task_id: str, session_id: str, submission: AttestationSubmission
) -> tuple[TaskAttestation, bool]:
    """Serialize evidence additions on their task, authenticating every new author.

    No kind, result, reference scheme or domain workflow has special meaning here.
    Corrections preserve the original record and explicitly supersede it.
    """
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = %s FOR UPDATE", (task_id,)).fetchone()
        if row is None:
            raise AttestationError("attestation task does not exist")
        task = Task.from_row(row)
        session = SessionManager(db).get(session_id)
        if session is None or session.project_id != task.project_id:
            raise AttestationError(
                "attestation author must be a registered session in this project"
            )
        definition = session_agent_definition(db, session)
        if definition is None:
            raise AttestationError("attestation author requires an enabled installed definition")
        body, digest = definition
        session_id = session.id
        task_id = task.id
        records = _records(conn, task_id)
        if submission.idempotency_key is not None:
            for record in records:
                if (record.session_id, record.idempotency_key) == (
                    session_id,
                    submission.idempotency_key,
                ):
                    original = AttestationSubmission.model_validate(
                        record.model_dump(include=set(AttestationSubmission.model_fields))
                    )
                    if not _same_content(original, submission):
                        raise AttestationError("idempotency key already names different evidence")
                    return record, False
        if not ({"*", submission.kind} & set(body.task_attestation_writes)):
            raise AttestationError("definition has no writer grant for this attestation kind")
        kind = body.task_attestation_kinds.get(submission.kind)
        if kind is None and (
            body.task_attestation_kinds or "*" not in body.task_attestation_writes
        ):
            raise AttestationError("attestation kind is not declared by this definition")
        if kind is not None and submission.result not in kind.allowed_results:
            raise AttestationError(
                f"Invalid result {submission.result!r} for {submission.kind}; "
                f"allowed results: {', '.join(kind.allowed_results)}. {kind.guidance}"
            )
        if refusal := task_scope_refusal(task, session_id, body.task_attestation_scope, db=db):
            raise AttestationError(refusal)
        if task.closed_at is not None:
            raise AttestationError("closed tasks cannot receive new attestations")
        rules = RuleDefinitionManager(db).list_effective(task.project_id)
        if authorship_refusal := attestation_authorship_refusal(
            kind=submission.kind,
            session_id=session_id,
            records=records,
            agent=body,
            rules=rules,
        ):
            raise AttestationError(authorship_refusal)
        reference = submission.reference
        if shape_refusal := attestation_shape_refusal(
            kind=submission.kind,
            result=submission.result,
            scheme=reference.scheme if reference is not None else None,
            value=reference.value if reference is not None else None,
            task=task.to_dict(),
            agent=body,
            declared=kind,
            rules=rules,
        ):
            raise AttestationError(shape_refusal)
        if submission.supersedes_id is not None:
            previous = next((r for r in records if r.id == submission.supersedes_id), None)
            if previous is None or previous.kind != submission.kind:
                raise AttestationError("correction must name the same task and attestation kind")
            if previous.session_id != session_id:
                raise AttestationError("only the original author may correct an attestation")
            if any(r.supersedes_id == previous.id for r in records):
                raise AttestationError("attestation was already superseded")
        now = utc_now()
        record = TaskAttestation(
            **submission.model_dump(),
            id=str(uuid4()),
            task_id=task_id,
            sequence=records[-1].sequence + 1 if records else 1,
            session_id=session_id,
            agent_definition=body.name,
            agent_definition_hash=digest,
            agent_capabilities=tuple(body.capabilities),
            created_at=now,
        )
        conn.execute(
            "INSERT INTO task_comments (id, task_id, author, author_type, body) "
            "VALUES (%s, %s, %s, %s, %s)",
            (record.id, task_id, session_id, AUTHOR_TYPE, record.model_dump_json()),
        )
        conn.execute("UPDATE tasks SET updated_at = %s WHERE id = %s", (now, task_id))
        advance_claim_flow(db, conn, task, record, [*records, record])
    return record, True
