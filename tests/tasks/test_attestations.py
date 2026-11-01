"""Generic evidence is independent of coding workflows and repository state."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from pydantic import ValidationError

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.attestations import (
    AttestationError,
    AttestationSubmission,
    TaskAttestation,
    attestations_by_task,
    list_attestations,
    record_attestation,
    restore_attestations,
)
from tests.fixtures.agent_sessions import AgentSessions as _Seats

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("same_key", [False, True], ids=["independent-appends", "same-key-replay"])
def test_concurrent_attestations_keep_one_ordered_history(
    temp_db: HubDatabase, sample_project: dict[str, Any], same_key: bool
) -> None:
    author = _Seats(temp_db, sample_project["id"]).add("concurrent-author", ["observation"])
    task = LocalTaskManager(temp_db).create_task(sample_project["id"], "Record observations")
    barrier = Barrier(4)

    def append(index: int) -> tuple[TaskAttestation, bool]:
        submission = AttestationSubmission(
            kind="observation",
            result="recorded",
            summary="Observed the artifact.",
            idempotency_key="shared" if same_key else f"observation-{index}",
        )
        barrier.wait(timeout=30)
        return record_attestation(
            temp_db, task_id=task.id, session_id=author, submission=submission
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(append, index) for index in range(4)]
        results = [future.result(timeout=30) for future in futures]

    count = 1 if same_key else 4
    assert sum(created for _, created in results) == count
    assert len({record.id for record, _ in results}) == count
    history = list_attestations(temp_db, task.id)
    assert [record.sequence for record in history] == list(range(1, count + 1))
    assert {record.id for record in history} == {record.id for record, _ in results}


@pytest.mark.parametrize("divergence", ["summary", "json-scalar-type"])
def test_generic_backup_restores_exact_history_without_rewriting_it(
    temp_db: HubDatabase, sample_project: dict[str, Any], divergence: str
) -> None:
    author = _Seats(temp_db, sample_project["id"]).add(
        "backup-author", [], definition_fields={"task_attestation_writes": ["*"]}
    )
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"], "Evidence backup", validation_criteria="Preserve original evidence."
    )
    original, _ = record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(
            kind="taste", result="needs-work", summary="Too sweet.", evidence={"verified": True}
        ),
    )
    corrected, _ = record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(
            kind="taste", result="pass", summary="Adjusted recipe.", supersedes_id=original.id
        ),
    )
    history = attestations_by_task(temp_db)[task.id]
    assert history == [original, corrected]
    with temp_db.transaction() as conn:
        conn.execute("DELETE FROM task_comments WHERE task_id = %s", (task.id,))
        assert restore_attestations(conn, task.id, history) == "extended"
        assert restore_attestations(conn, task.id, history) == "unchanged"
        assert restore_attestations(conn, task.id, [original]) == "ahead"
    assert list_attestations(temp_db, task.id) == history
    with temp_db.transaction() as conn:
        conn.execute("DELETE FROM task_comments WHERE id = %s", (corrected.id,))
    update: dict[str, Any] = (
        {"summary": "Rewritten history."}
        if divergence == "summary"
        else {"evidence": {"verified": 1}}
    )
    changed = [original.model_copy(update=update), corrected]
    with temp_db.transaction() as conn:
        assert restore_attestations(conn, task.id, changed) == "diverged"
    assert list_attestations(temp_db, task.id) == [original]


@pytest.mark.parametrize(
    "invalid",
    ["other-task", "sequence-gap", "missing-superseded-record", "uppercase-id", "uppercase-author"],
)
def test_restore_rejects_invalid_history_before_appending_anything(
    temp_db: HubDatabase, sample_project: dict[str, Any], invalid: str
) -> None:
    author = _Seats(temp_db, sample_project["id"]).add(
        "restore-author", [], definition_fields={"task_attestation_writes": ["*"]}
    )
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"], "Invalid backup", validation_criteria="Reject malformed history."
    )
    record, _ = record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(kind="arbitrary", result="pass", summary="Original."),
    )
    cases: dict[str, dict[str, Any]] = {
        "other-task": {"task_id": "another-task"},
        "sequence-gap": {"sequence": 2},
        "missing-superseded-record": {"supersedes_id": "missing-record"},
        "uppercase-id": {"id": "ABCDEFAB-1234-4ABC-ABCD-ABCDEFABCDEF"},
        "uppercase-author": {"session_id": "ABCDEFAB-1234-4ABC-ABCD-ABCDEFABCDEF"},
    }
    malformed = record.model_copy(update=cases[invalid])
    with temp_db.transaction() as conn:
        conn.execute("DELETE FROM task_comments WHERE task_id = %s", (task.id,))
        with pytest.raises(AttestationError):
            restore_attestations(conn, task.id, [malformed])
    assert list_attestations(temp_db, task.id) == []


def test_idempotent_replay_distinguishes_json_boolean_from_number(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    author = _Seats(temp_db, sample_project["id"]).add("json-author", ["check"])
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"], "Exact JSON", validation_criteria="Preserve evidence types."
    )
    submission = AttestationSubmission(
        kind="check",
        result="pass",
        summary="Checked.",
        idempotency_key="json-value",
        evidence={"verified": True},
    )
    original, _ = record_attestation(
        temp_db, task_id=task.id, session_id=author, submission=submission
    )
    with pytest.raises(AttestationError, match="different evidence"):
        record_attestation(
            temp_db,
            task_id=task.id,
            session_id=author,
            submission=submission.model_copy(update={"evidence": {"verified": 1}}),
        )
    assert list_attestations(temp_db, task.id) == [original]


def test_stored_evidence_rejects_nonfinite_json_before_it_can_normalize_to_null() -> None:
    with pytest.raises(ValidationError, match="finite"):
        TaskAttestation.model_validate(
            {
                "id": "abcdefab-1234-4abc-abcd-abcdefabcdef",
                "task_id": "12345678-1234-4234-8234-123456789abc",
                "session_id": "fedcbafe-1234-4abc-abcd-abcdefabcdef",
                "kind": "sensor",
                "result": "measured",
                "summary": "Nonfinite backup.",
                "evidence": {"reading": float("nan")},
                "sequence": 1,
                "agent_definition": "sensor-author",
                "agent_definition_hash": "a" * 64,
                "agent_capabilities": [],
                "created_at": "2026-11-01T00:00:00+00:00",
            }
        )


def test_backup_keeps_idempotency_keys_scoped_to_their_authors(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    seats = _Seats(temp_db, sample_project["id"])
    authors = [
        seats.add(name, [], definition_fields={"task_attestation_writes": ["*"]})
        for name in ("backup-first-author", "backup-second-author")
    ]
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"], "Author keys", validation_criteria="Keys belong to authors."
    )
    for author in authors:
        record_attestation(
            temp_db,
            task_id=task.id,
            session_id=author,
            submission=AttestationSubmission(
                kind="check", result="pass", summary="Checked.", idempotency_key="same-key"
            ),
        )
    history = attestations_by_task(temp_db)[task.id]
    assert [record.session_id for record in history] == authors
    assert list_attestations(temp_db, task.id) == history


def test_non_git_evidence_has_authenticated_provenance_and_exact_replay(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    seats = _Seats(temp_db, sample_project["id"])
    author = seats.add("pastry-author", [], definition_fields={"task_attestation_writes": ["*"]})
    task = LocalTaskManager(temp_db).create_task(
        title="Bake cake",
        project_id=sample_project["id"],
        category="manual",
        validation_criteria="Cake reaches the specified center temperature.",
    )
    submission = AttestationSubmission(
        kind="oven-temperature",
        result="measured",
        summary="Center reached the specified temperature.",
        evidence={"celsius": 96, "sensor": "probe-7"},
        reference={"scheme": "artifact", "value": "kitchen://cake/42"},
        idempotency_key="probe-reading-1",
    )
    record, created = record_attestation(
        temp_db, task_id=task.id, session_id=author, submission=submission
    )
    assert created
    assert record.session_id == author
    assert record.agent_definition == "pastry-author"
    assert record.agent_definition_hash
    assert record.sequence == 1
    assert record.evidence == {"celsius": 96, "sensor": "probe-7"}
    assert record.reference == submission.reference
    replay, created = record_attestation(
        temp_db, task_id=task.id, session_id=author, submission=submission
    )
    assert not created
    assert replay == record
    assert list_attestations(temp_db, task.id) == [record]


def test_corrections_append_and_cannot_rewrite_other_authors_or_fork(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    seats = _Seats(temp_db, sample_project["id"])
    author = seats.add("author-a", [], definition_fields={"task_attestation_writes": ["*"]})
    other = seats.add("author-b", [], definition_fields={"task_attestation_writes": ["*"]})
    task = LocalTaskManager(temp_db).create_task(
        title="Draft book",
        project_id=sample_project["id"],
        validation_criteria="All chapters are proofread.",
    )
    original, _ = record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(kind="proofread", result="pass", summary="First pass."),
    )
    correction = AttestationSubmission(
        kind="proofread",
        result="fail",
        summary="Found a missing chapter.",
        supersedes_id=original.id,
    )
    with pytest.raises(AttestationError, match="author"):
        record_attestation(temp_db, task_id=task.id, session_id=other, submission=correction)
    corrected, _ = record_attestation(
        temp_db, task_id=task.id, session_id=author, submission=correction
    )
    assert corrected.sequence == 2
    assert corrected.supersedes_id == original.id
    assert list_attestations(temp_db, task.id) == [original, corrected]
    with pytest.raises(AttestationError, match="superseded"):
        record_attestation(temp_db, task_id=task.id, session_id=author, submission=correction)
    assert list_attestations(temp_db, task.id)[0].result == "pass"


@pytest.mark.parametrize(
    "scope",
    [
        {"task_types": ["research_spike"]},
        {"categories": ["research"]},
        {"own_claim_only": True},
    ],
)
def test_attestation_scope_is_independent_of_retired_step_permissions(
    temp_db: HubDatabase, sample_project: dict[str, Any], scope: dict[str, Any]
) -> None:
    seats = _Seats(temp_db, sample_project["id"])
    author = seats.add(
        "cake-inspector", ["taste"], definition_fields={"task_attestation_scope": scope}
    )
    task = LocalTaskManager(temp_db).create_task(
        title="Inspect cake", project_id=sample_project["id"], validation_criteria="Balanced taste."
    )
    with pytest.raises(AttestationError, match="scope"):
        record_attestation(
            temp_db,
            task_id=task.id,
            session_id=author,
            submission=AttestationSubmission(kind="taste", result="pass", summary="Balanced."),
        )
    assert list_attestations(temp_db, task.id) == []


def test_writer_grants_and_idempotency_are_integrity_checks(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    seats = _Seats(temp_db, sample_project["id"])
    author = seats.add(
        "limited-author", [], definition_fields={"task_attestation_writes": ["taste"]}
    )
    task = LocalTaskManager(temp_db).create_task(
        title="Taste cake",
        project_id=sample_project["id"],
        validation_criteria="Taste is balanced.",
    )
    with pytest.raises(AttestationError, match="grant"):
        record_attestation(
            temp_db,
            task_id=task.id,
            session_id=author,
            submission=AttestationSubmission(kind="publish", result="pass", summary="Published."),
        )
    first = AttestationSubmission(
        kind="taste", result="pass", summary="Balanced sweetness.", idempotency_key="taste-1"
    )
    record_attestation(temp_db, task_id=task.id, session_id=author, submission=first)
    with pytest.raises(AttestationError, match="idempotency"):
        record_attestation(
            temp_db,
            task_id=task.id,
            session_id=author,
            submission=first.model_copy(update={"result": "fail"}),
        )
    assert len(list_attestations(temp_db, task.id)) == 1


@pytest.mark.parametrize("field", ["session_id", "agent_definition", "created_at", "sequence"])
def test_client_cannot_supply_provenance(field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        AttestationSubmission.model_validate(
            {"kind": "implementation", "result": "done", "summary": "Complete.", field: "forged"}
        )


@pytest.mark.parametrize("field", ["kind", "result", "summary"])
def test_blank_evidence_labels_are_invalid(field: str) -> None:
    with pytest.raises(ValidationError, match="non-whitespace"):
        AttestationSubmission.model_validate(
            {"kind": "taste", "result": "pass", "summary": "Balanced.", field: " \n "}
        )


def test_evidence_payload_has_a_storage_bound() -> None:
    with pytest.raises(ValidationError, match="1048576"):
        AttestationSubmission(
            kind="taste", result="pass", summary="Checked.", evidence={"unbounded": "x" * 1048576}
        )


@pytest.mark.parametrize(
    "reading",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "infinity", "negative-infinity"],
)
def test_nonfinite_evidence_cannot_silently_change_on_serialization(reading: float) -> None:
    with pytest.raises(ValidationError, match="finite"):
        AttestationSubmission(
            kind="temperature",
            result="measured",
            summary="Read sensor.",
            evidence={"sensor": {"samples": [reading]}},
        )
