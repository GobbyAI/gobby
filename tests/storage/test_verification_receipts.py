from __future__ import annotations

import hashlib

import pytest

from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.verification_receipts import (
    VerificationOutcome,
    VerificationReceiptStore,
    VerificationReceiptWrite,
)
from gobby.utils.datetime import utc_now


def _write(
    *,
    project_id: str,
    session_id: str,
    execution_id: str,
    task_id: str | None = None,
    outcome: VerificationOutcome = "success",
    command: str = "uv run pytest tests/example.py -q",
    output: str | None = None,
    terminal: bool = True,
) -> VerificationReceiptWrite:
    now = utc_now()
    return VerificationReceiptWrite(
        project_id=project_id,
        session_id=session_id,
        task_id=task_id,
        provider="codex",
        execution_id=execution_id,
        source_event_id=f"event:{execution_id}",
        evidence_type="validation_command",
        command=command,
        cwd="/repo",
        normalized_outcome=outcome,
        outcome_provenance="structured_exit_code" if terminal else "before_tool",
        exit_code=(
            0
            if outcome == "success" and terminal
            else 1
            if outcome == "failure" and terminal
            else None
        ),
        started_at=now,
        completed_at=now if terminal else None,
        output=output,
        attribution_source="sole_claim" if task_id else "unassigned",
        attribution_actor=session_id if task_id else None,
        attributed_at=now if task_id else None,
    )


@pytest.fixture
def task_manager(temp_db) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


def _session(session_manager: SessionManager, project_id: str, suffix: str):
    return session_manager.register(
        external_id=f"receipt-{suffix}",
        machine_id="machine-1",
        source="codex",
        project_id=project_id,
        title=f"Receipt {suffix}",
    )


def test_provisional_terminal_upsert_and_distinct_identical_commands(
    temp_db, session_manager, sample_project, task_manager
) -> None:
    session = _session(session_manager, sample_project["id"], "upsert")
    task = task_manager.create_task(
        sample_project["id"],
        "Receipt task",
        claimed_by_session_id=session.id,
        category="code",
    )
    store = VerificationReceiptStore(temp_db)

    provisional = store.upsert(
        _write(
            project_id=sample_project["id"],
            session_id=session.id,
            execution_id="call-1",
            task_id=task.id,
            outcome="provisional",
            terminal=False,
        )
    )
    output = "head\n" + ("x" * 10_000) + "\ntail"
    terminal = store.upsert(
        _write(
            project_id=sample_project["id"],
            session_id=session.id,
            execution_id="call-1",
            task_id=task.id,
            output=output,
        )
    )
    duplicate = store.upsert(
        _write(
            project_id=sample_project["id"],
            session_id=session.id,
            execution_id="call-1",
            task_id=task.id,
            output=output,
        )
    )
    unknown_duplicate = store.upsert(
        _write(
            project_id=sample_project["id"],
            session_id=session.id,
            execution_id="call-1",
            task_id=task.id,
            outcome="unknown",
        )
    )
    conflicting = store.upsert(
        _write(
            project_id=sample_project["id"],
            session_id=session.id,
            execution_id="call-1",
            task_id=task.id,
            outcome="failure",
        )
    )
    provisional_after_conflict = store.upsert(
        _write(
            project_id=sample_project["id"],
            session_id=session.id,
            execution_id="call-1",
            task_id=task.id,
            outcome="provisional",
            terminal=False,
        )
    )
    second = store.upsert(
        _write(
            project_id=sample_project["id"],
            session_id=session.id,
            execution_id="call-2",
            task_id=task.id,
            output=output,
        )
    )

    assert provisional.id == terminal.id == duplicate.id
    assert unknown_duplicate.normalized_outcome == "success"
    assert conflicting.normalized_outcome == "conflicting"
    assert provisional_after_conflict.normalized_outcome == "conflicting"
    assert second.id != terminal.id
    assert terminal.normalized_outcome == "success"
    assert terminal.completed_at is not None
    assert len(terminal.output_first_4k or "") <= 4096
    assert len(terminal.output_last_4k or "") <= 4096
    assert terminal.output_sha256 == hashlib.sha256(output.encode()).hexdigest()
    assert terminal.output_bytes == len(output.encode())
    assert len(store.list_for_task(sample_project["id"], task.id)) == 2


def test_more_than_fifty_receipts_are_durable(
    temp_db, session_manager, sample_project, task_manager
) -> None:
    session = _session(session_manager, sample_project["id"], "many")
    task = task_manager.create_task(
        sample_project["id"], "Many receipts", claimed_by_session_id=session.id
    )
    store = VerificationReceiptStore(temp_db)

    for index in range(55):
        store.upsert(
            _write(
                project_id=sample_project["id"],
                session_id=session.id,
                execution_id=f"call-{index}",
                task_id=task.id,
            )
        )

    assert len(store.list_for_task(sample_project["id"], task.id)) == 55


def test_attribution_prefers_valid_active_then_sole_claim_and_rejects_ambiguity(
    temp_db, session_manager, sample_project, task_manager
) -> None:
    session = _session(session_manager, sample_project["id"], "attribution")
    first = task_manager.create_task(
        sample_project["id"], "First", claimed_by_session_id=session.id
    )
    second = task_manager.create_task(
        sample_project["id"], "Second", claimed_by_session_id=session.id
    )
    store = VerificationReceiptStore(temp_db)

    assert store.resolve_attribution(
        project_id=sample_project["id"], session_id=session.id, active_task_ref=first.id
    ) == (first.id, "active_task")
    assert store.resolve_attribution(
        project_id=sample_project["id"], session_id=session.id, active_task_ref="missing"
    ) == (None, "unassigned")

    task_manager.release_task_claim(second.id)
    assert store.resolve_attribution(project_id=sample_project["id"], session_id=session.id) == (
        first.id,
        "sole_claim",
    )


def test_inspection_assignment_is_isolated_and_one_way(
    temp_db, session_manager, sample_project, task_manager
) -> None:
    other_project = LocalProjectManager(temp_db).create("receipt-other")
    session = _session(session_manager, sample_project["id"], "assignment")
    other_session = _session(session_manager, other_project.id, "other")
    task = task_manager.create_task(
        sample_project["id"], "Assignment", claimed_by_session_id=session.id
    )
    other_task = task_manager.create_task(
        other_project.id, "Other assignment", claimed_by_session_id=other_session.id
    )
    store = VerificationReceiptStore(temp_db)
    local = store.upsert(
        _write(
            project_id=sample_project["id"],
            session_id=session.id,
            execution_id="local",
        )
    )
    other = store.upsert(
        _write(
            project_id=other_project.id,
            session_id=other_session.id,
            execution_id="other",
        )
    )

    page, total = store.list_page(
        project_id=sample_project["id"],
        session_id=session.id,
        scope="unassigned",
        task_id=None,
        limit=1,
        offset=0,
    )
    assert total == 1
    assert [receipt.id for receipt in page] == [local.id]

    assigned = store.assign_unassigned(
        project_id=sample_project["id"],
        session_id=session.id,
        task_id=task.id,
        receipt_ids=[local.id],
        actor=f"session:{session.id}",
    )
    assert assigned[0].task_id == task.id
    assert assigned[0].attribution_source == "manual_assignment"
    with pytest.raises(ValueError, match="already assigned"):
        store.assign_unassigned(
            project_id=sample_project["id"],
            session_id=session.id,
            task_id=task.id,
            receipt_ids=[local.id],
            actor=f"session:{session.id}",
        )
    with pytest.raises(ValueError, match="current project and session"):
        store.assign_unassigned(
            project_id=sample_project["id"],
            session_id=session.id,
            task_id=task.id,
            receipt_ids=[other.id],
            actor=f"session:{session.id}",
        )
    assert store.list_for_task(other_project.id, other_task.id) == []


def test_task_and_unassigned_receipts_follow_separate_retention(
    temp_db, session_manager, sample_project, task_manager
) -> None:
    session = _session(session_manager, sample_project["id"], "retention")
    task = task_manager.create_task(
        sample_project["id"], "Retention", claimed_by_session_id=session.id
    )
    store = VerificationReceiptStore(temp_db)
    assigned = store.upsert(
        _write(
            project_id=sample_project["id"],
            session_id=session.id,
            execution_id="assigned",
            task_id=task.id,
        )
    )
    unassigned = store.upsert(
        _write(
            project_id=sample_project["id"],
            session_id=session.id,
            execution_id="unassigned",
        )
    )

    assert session_manager.delete(session.id) is True
    assert temp_db.fetchone("SELECT id FROM verification_receipts WHERE id = %s", (assigned.id,))
    assert (
        temp_db.fetchone("SELECT id FROM verification_receipts WHERE id = %s", (unassigned.id,))
        is None
    )

    assert task_manager.delete_task(task.id) is True
    assert (
        temp_db.fetchone("SELECT id FROM verification_receipts WHERE id = %s", (assigned.id,))
        is None
    )
