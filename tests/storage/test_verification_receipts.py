from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.verification_receipts import (
    VerificationOutcome,
    VerificationReceiptStore,
    VerificationReceiptWrite,
)
from gobby.storage.worktrees import LocalWorktreeManager
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
        validation_epoch=0 if task_id else None,
        attribution_source="sole_claim" if task_id else "unassigned",
        attribution_actor=session_id if task_id else None,
        attributed_at=now if task_id else None,
    )


@pytest.fixture
def task_manager(temp_db: HubDatabase) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


def _session(session_manager: SessionManager, project_id: str, suffix: str) -> Session:
    return session_manager.register(
        external_id=f"receipt-{suffix}",
        machine_id="machine-1",
        source="codex",
        project_id=project_id,
        title=f"Receipt {suffix}",
    )


def test_pending_terminal_upsert_and_distinct_identical_commands(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    task_manager: LocalTaskManager,
) -> None:
    session = _session(session_manager, sample_project["id"], "upsert")
    task = task_manager.create_task(
        sample_project["id"],
        "Receipt task",
        claimed_by_session_id=session.id,
        category="code",
        validation_criteria="The attributed receipt has the expected terminal outcome.",
    )
    store = VerificationReceiptStore(temp_db)

    pending = store.upsert(
        _write(
            project_id=sample_project["id"],
            session_id=session.id,
            execution_id="call-1",
            task_id=task.id,
            outcome="pending",
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
    pending_after_conflict = store.upsert(
        _write(
            project_id=sample_project["id"],
            session_id=session.id,
            execution_id="call-1",
            task_id=task.id,
            outcome="pending",
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

    assert pending.id == terminal.id == duplicate.id
    assert unknown_duplicate.normalized_outcome == "success"
    assert conflicting.normalized_outcome == "conflicting"
    assert pending_after_conflict.normalized_outcome == "conflicting"
    assert second.id != terminal.id
    assert terminal.normalized_outcome == "success"
    assert terminal.completed_at is not None
    assert len(terminal.output_first_4k or "") <= 4096
    assert len(terminal.output_last_4k or "") <= 4096
    assert terminal.output_sha256 == hashlib.sha256(output.encode()).hexdigest()
    assert terminal.output_bytes == len(output.encode())
    assert len(store.list_for_task(sample_project["id"], task.id)) == 2


def test_more_than_fifty_receipts_are_durable(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    task_manager: LocalTaskManager,
) -> None:
    session = _session(session_manager, sample_project["id"], "many")
    task = task_manager.create_task(
        sample_project["id"],
        "Many receipts",
        claimed_by_session_id=session.id,
        validation_criteria="All attributed receipts remain durable.",
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
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    task_manager: LocalTaskManager,
) -> None:
    session = _session(session_manager, sample_project["id"], "attribution")
    first = task_manager.create_task(
        sample_project["id"],
        "First",
        claimed_by_session_id=session.id,
        validation_criteria="Attribution resolves to this task when it is active.",
    )
    second = task_manager.create_task(
        sample_project["id"],
        "Second",
        claimed_by_session_id=session.id,
        validation_criteria="Attribution resolves to this task when it is active.",
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


def test_worktree_attribution_matches_exact_and_nested_paths(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    task_manager: LocalTaskManager,
    tmp_path: Path,
) -> None:
    session = _session(session_manager, sample_project["id"], "worktree-match")
    task = task_manager.create_task(
        sample_project["id"],
        "Worktree task",
        claimed_by_session_id=session.id,
        validation_criteria="Exact and nested worktree paths resolve to this task.",
    )
    worktree_path = tmp_path / "task-worktree"
    LocalWorktreeManager(temp_db).create(
        sample_project["id"],
        "task-worktree",
        str(worktree_path),
        task_id=task.id,
    )
    store = VerificationReceiptStore(temp_db)

    assert store.resolve_attribution(
        project_id=sample_project["id"],
        session_id=session.id,
        execution_cwd=str(worktree_path),
    ) == (task.id, "worktree_task")
    assert store.resolve_attribution(
        project_id=sample_project["id"],
        session_id=session.id,
        execution_cwd=str(worktree_path / "nested" / "directory"),
    ) == (task.id, "worktree_task")


def test_attribution_order_prefers_explicit_worktree_session_then_active(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    task_manager: LocalTaskManager,
    tmp_path: Path,
) -> None:
    session = _session(session_manager, sample_project["id"], "attribution-order")
    tasks = [
        task_manager.create_task(
            sample_project["id"],
            title,
            claimed_by_session_id=session.id,
            validation_criteria=f"{title} participates in attribution ordering.",
        )
        for title in ("Worktree", "Session context", "Active variable", "Explicit")
    ]
    worktree_task, session_task, active_task, explicit_task = tasks
    worktree_path = tmp_path / "ordered-worktree"
    LocalWorktreeManager(temp_db).create(
        sample_project["id"],
        "ordered-worktree",
        str(worktree_path),
        task_id=worktree_task.id,
    )
    store = VerificationReceiptStore(temp_db)
    arguments = {
        "project_id": sample_project["id"],
        "session_id": session.id,
        "active_task_ref": active_task.id,
        "session_task_ref": session_task.id,
        "execution_cwd": str(worktree_path),
    }

    assert store.resolve_attribution(
        **arguments,
        explicit_task_ref=explicit_task.id,
    ) == (explicit_task.id, "explicit_task")
    assert store.resolve_attribution(**arguments) == (worktree_task.id, "worktree_task")
    assert store.resolve_attribution(
        project_id=sample_project["id"],
        session_id=session.id,
        active_task_ref=active_task.id,
        session_task_ref=session_task.id,
    ) == (session_task.id, "active_task")
    assert store.resolve_attribution(
        project_id=sample_project["id"],
        session_id=session.id,
        active_task_ref=active_task.id,
    ) == (active_task.id, "active_task")


@pytest.mark.parametrize(
    "cwd_kind",
    ("relative", "unregistered", "sibling_prefix", "malformed"),
)
def test_worktree_attribution_rejects_unsafe_execution_paths(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    task_manager: LocalTaskManager,
    tmp_path: Path,
    cwd_kind: str,
) -> None:
    session = _session(session_manager, sample_project["id"], f"unsafe-{cwd_kind}")
    worktree_task = task_manager.create_task(
        sample_project["id"],
        "Registered worktree",
        claimed_by_session_id=session.id,
        validation_criteria="Only a contained absolute path resolves to this task.",
    )
    fallback_task = task_manager.create_task(
        sample_project["id"],
        "Active fallback",
        claimed_by_session_id=session.id,
        validation_criteria="Rejected worktree paths continue to active-task attribution.",
    )
    worktree_path = tmp_path / "agent-1"
    LocalWorktreeManager(temp_db).create(
        sample_project["id"],
        f"unsafe-{cwd_kind}",
        str(worktree_path),
        task_id=worktree_task.id,
    )
    execution_cwds = {
        "relative": "agent-1",
        "unregistered": str(tmp_path / "missing"),
        "sibling_prefix": str(tmp_path / "agent-1-sibling"),
        "malformed": f"{tmp_path}/bad\x00path",
    }

    assert VerificationReceiptStore(temp_db).resolve_attribution(
        project_id=sample_project["id"],
        session_id=session.id,
        active_task_ref=fallback_task.id,
        execution_cwd=execution_cwds[cwd_kind],
    ) == (fallback_task.id, "active_task")


@pytest.mark.parametrize("candidate_state", ("unclaimed", "closed"))
def test_worktree_attribution_rejects_ineligible_tasks(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    task_manager: LocalTaskManager,
    tmp_path: Path,
    candidate_state: str,
) -> None:
    session = _session(session_manager, sample_project["id"], f"ineligible-{candidate_state}")
    candidate = task_manager.create_task(
        sample_project["id"],
        "Ineligible worktree task",
        claimed_by_session_id=session.id if candidate_state == "closed" else None,
        validation_criteria="An ineligible worktree task is rejected.",
    )
    if candidate_state == "closed":
        task_manager.close_task(candidate.id, force=True)
    fallback_task = task_manager.create_task(
        sample_project["id"],
        "Eligible active fallback",
        claimed_by_session_id=session.id,
        validation_criteria="An eligible active task receives fallback attribution.",
    )
    worktree_path = tmp_path / candidate_state
    LocalWorktreeManager(temp_db).create(
        sample_project["id"],
        f"ineligible-{candidate_state}",
        str(worktree_path),
        task_id=candidate.id,
    )

    store = VerificationReceiptStore(temp_db)
    assert store.resolve_attribution(
        project_id=sample_project["id"],
        session_id=session.id,
        active_task_ref=fallback_task.id,
        execution_cwd=str(worktree_path),
    ) == (fallback_task.id, "active_task")
    assert store.resolve_attribution(
        project_id=sample_project["id"],
        session_id=session.id,
        execution_cwd=str(worktree_path),
    ) == (fallback_task.id, "sole_claim")


def test_worktree_attribution_rejects_ambiguous_paths(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    task_manager: LocalTaskManager,
    tmp_path: Path,
) -> None:
    session = _session(session_manager, sample_project["id"], "ambiguous-worktree")
    parent_task, nested_task, fallback_task = [
        task_manager.create_task(
            sample_project["id"],
            title,
            claimed_by_session_id=session.id,
            validation_criteria=f"{title} is used to test ambiguous worktree containment.",
        )
        for title in ("Parent worktree", "Nested worktree", "Active fallback")
    ]
    parent_path = tmp_path / "ambiguous"
    nested_path = parent_path / "nested"
    worktree_manager = LocalWorktreeManager(temp_db)
    worktree_manager.create(
        sample_project["id"],
        "ambiguous-parent",
        str(parent_path),
        task_id=parent_task.id,
    )
    worktree_manager.create(
        sample_project["id"],
        "ambiguous-nested",
        str(nested_path),
        task_id=nested_task.id,
    )

    assert VerificationReceiptStore(temp_db).resolve_attribution(
        project_id=sample_project["id"],
        session_id=session.id,
        active_task_ref=fallback_task.id,
        execution_cwd=str(nested_path),
    ) == (fallback_task.id, "active_task")


def test_worktree_attribution_rejects_cross_project_path(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    task_manager: LocalTaskManager,
    tmp_path: Path,
) -> None:
    session = _session(session_manager, sample_project["id"], "cross-project-worktree")
    fallback_task = task_manager.create_task(
        sample_project["id"],
        "Current project fallback",
        claimed_by_session_id=session.id,
        validation_criteria="Cross-project paths continue to current-project fallback attribution.",
    )
    other_project = LocalProjectManager(temp_db).create(
        "receipt-cross-project",
        repo_path=str(tmp_path / "other-project"),
    )
    other_session = _session(session_manager, other_project.id, "cross-project-owner")
    other_task = task_manager.create_task(
        other_project.id,
        "Other project worktree",
        claimed_by_session_id=other_session.id,
        validation_criteria="This task cannot authorize receipts from another project.",
    )
    worktree_path = tmp_path / "other-project" / "worktree"
    LocalWorktreeManager(temp_db).create(
        other_project.id,
        "other-project-worktree",
        str(worktree_path),
        task_id=other_task.id,
    )

    assert VerificationReceiptStore(temp_db).resolve_attribution(
        project_id=sample_project["id"],
        session_id=session.id,
        active_task_ref=fallback_task.id,
        execution_cwd=str(worktree_path),
    ) == (fallback_task.id, "active_task")


def test_inspection_assignment_is_isolated_and_one_way(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    task_manager: LocalTaskManager,
) -> None:
    other_project = LocalProjectManager(temp_db).create("receipt-other")
    session = _session(session_manager, sample_project["id"], "assignment")
    other_session = _session(session_manager, other_project.id, "other")
    task = task_manager.create_task(
        sample_project["id"],
        "Assignment",
        claimed_by_session_id=session.id,
        validation_criteria="Receipt assignment remains project-scoped.",
    )
    other_task = task_manager.create_task(
        other_project.id,
        "Other assignment",
        claimed_by_session_id=other_session.id,
        validation_criteria="Receipt assignment remains project-scoped.",
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
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    task_manager: LocalTaskManager,
) -> None:
    session = _session(session_manager, sample_project["id"], "retention")
    task = task_manager.create_task(
        sample_project["id"],
        "Retention",
        claimed_by_session_id=session.id,
        validation_criteria="Assigned receipts follow task retention.",
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
