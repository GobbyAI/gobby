from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.storage.verification_receipts import VerificationReceipt
from gobby.tasks.criteria_contract import (
    TaskCriteriaError,
    split_validation_criteria,
)
from gobby.tasks.evidence_admission import admit_task_evidence
from gobby.tasks.task_state_evidence import build_linked_diff_evidence
from gobby.tasks.validation_verdict import _validation_result_from_data
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 25, tzinfo=UTC)


def _receipt(
    evidence_id: str,
    *,
    task_id: str = "task-1",
    outcome: str = "success",
    epoch: int | None = 3,
    provenance: str = "structured_exit_code",
    evidence_type: str = "shell_command",
    terminal: bool = True,
) -> VerificationReceipt:
    return VerificationReceipt(
        id=evidence_id,
        project_id="project-1",
        session_id="session-1",
        task_id=task_id,
        provider="codex",
        execution_id=f"execution:{evidence_id}",
        source_event_id=f"event:{evidence_id}",
        evidence_type=evidence_type,
        command="uv run pytest tests/focused.py -q",
        cwd="/repo",
        normalized_outcome=outcome,  # type: ignore[arg-type]
        outcome_provenance=provenance,
        exit_code=0 if outcome == "success" else 1 if outcome == "failure" else None,
        started_at=_NOW,
        completed_at=_NOW if terminal else None,
        output_first_4k="focused result",
        output_last_4k="focused result",
        output_sha256="0" * 64,
        output_bytes=14,
        details={},
        attribution_source="explicit_task",
        attribution_actor="session-1",
        attributed_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
        validation_epoch=epoch,
    )


def test_criteria_split_preserves_distinct_observable_items() -> None:
    assert split_validation_criteria(
        "1. Focused pytest exits zero.\n"
        "2. Ruff exits zero.\n"
        "   The final diff contains no suppression."
    ) == (
        "Focused pytest exits zero.",
        "Ruff exits zero. The final diff contains no suppression.",
    )


def test_non_epic_creation_and_update_require_criteria(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)

    with pytest.raises(TaskCriteriaError, match="Every non-epic task"):
        manager.create_task(sample_project["id"], "Missing contract")

    epic = manager.create_task(sample_project["id"], "Organizational epic", task_type="epic")
    task = manager.create_task(
        sample_project["id"],
        "Contract task",
        validation_criteria="The observable result is recorded.",
    )

    assert epic.validation_criteria is None
    with pytest.raises(TaskCriteriaError, match="Every non-epic task"):
        manager.update_task(task.id, validation_criteria=None)


def test_admission_keeps_only_current_trusted_successes() -> None:
    receipts = [
        _receipt("admitted"),
        _receipt("stale", epoch=2),
        _receipt("failed", outcome="failure"),
        _receipt("pending", outcome="pending", terminal=False),
        _receipt("unknown", outcome="unknown"),
        _receipt("untrusted", provenance="manual_attestation"),
        _receipt("wrong-task", task_id="task-2"),
    ]

    admission = admit_task_evidence(
        receipts,
        task_id="task-1",
        validation_epoch=3,
        validation_criteria="Focused pytest exits zero.",
    )

    assert admission.evidence_ids == frozenset({"admitted"})
    assert admission.audit_summary() == {
        "admissible_total": 1,
        "rejected_total": 6,
        "rejected_by_reason": {
            "failed": 1,
            "pending": 1,
            "stale_validation_epoch": 1,
            "unknown": 1,
            "untrusted_outcome": 1,
            "wrong_task": 1,
        },
    }


def test_criterion_verdict_requires_complete_real_citations() -> None:
    criteria = ["Focused pytest exits zero.", "Ruff exits zero."]
    result = _validation_result_from_data(
        {
            "status": "valid",
            "feedback": "Both gates passed.",
            "blocking_reasons": [],
            "issues": [],
            "current_failure_evidence": [],
            "criterion_results": [
                {
                    "criterion": criteria[0],
                    "status": "satisfied",
                    "evidence_ids": ["pytest-receipt"],
                    "explanation": "The structured pytest exit is zero.",
                },
                {
                    "criterion": criteria[1],
                    "status": "satisfied",
                    "evidence_ids": ["ruff-receipt"],
                    "explanation": "The structured Ruff exit is zero.",
                },
            ],
        },
        expected_criteria=criteria,
        admissible_evidence_ids=["pytest-receipt", "ruff-receipt"],
    )

    assert result.status == "valid"
    assert [item.evidence_ids for item in result.criterion_results] == [
        ["pytest-receipt"],
        ["ruff-receipt"],
    ]


def test_criterion_verdict_fails_closed_on_missing_and_invented_evidence() -> None:
    criteria = ["Rendered document is correct.", "Links are valid."]
    result = _validation_result_from_data(
        {
            "status": "valid",
            "feedback": "Looks complete.",
            "blocking_reasons": [],
            "issues": [],
            "current_failure_evidence": [],
            "criterion_results": [
                {
                    "criterion": criteria[0],
                    "status": "satisfied",
                    "evidence_ids": ["invented"],
                    "explanation": "A render exists.",
                }
            ],
        },
        expected_criteria=criteria,
        admissible_evidence_ids=[],
    )

    assert result.status == "invalid"
    assert result.criterion_results[0].status == "gap"
    assert result.criterion_results[1].status == "gap"
    assert any("outside the admissible packet" in reason for reason in result.blocking_reasons)
    assert any("did not cover" in reason for reason in result.blocking_reasons)


def test_successful_task_edit_advances_validation_epoch(
    temp_db: Any,
    sample_project: dict[str, Any],
    session_manager: Any,
) -> None:
    session = session_manager.register(
        external_id="epoch-session",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Epoch task",
        claimed_by_session_id=session.id,
        validation_criteria="Focused evidence is current after the final edit.",
    )
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(session.id, {"claimed_tasks": {task.id: "#1"}})

    variables.record_edited_file(
        session.id,
        "src/first.py",
        condition_name="verification_evidence_recorded",
        updates={},
    )
    assert manager.get_task(task.id).validation_epoch == 1

    variables.record_edited_file(
        session.id,
        "src/second.py",
        condition_name="verification_evidence_recorded",
        updates={},
    )
    assert manager.get_task(task.id).validation_epoch == 2


@pytest.mark.parametrize(
    ("category", "expected_type"),
    [
        ("code", "linked_diff"),
        ("config", "linked_diff"),
        ("docs", "document_artifact"),
        ("research", "research_artifact"),
        ("planning", "plan_artifact"),
    ],
)
def test_final_task_state_evidence_is_stable_and_category_specific(
    temp_db: Any,
    sample_project: dict[str, Any],
    category: str,
    expected_type: str,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        f"{category} evidence task",
        category=category,
        validation_criteria="The final task artifact is present.",
    )
    linked_task = replace(task, commits=["abc123"])

    first = build_linked_diff_evidence(
        linked_task,
        session_id="00000000-0000-0000-0000-000000000001",
        validation_context="final state one",
        observed_at=datetime(2026, 7, 25, tzinfo=UTC),
    )
    second = build_linked_diff_evidence(
        linked_task,
        session_id="00000000-0000-0000-0000-000000000001",
        validation_context="final state two",
        observed_at=datetime(2026, 7, 25, 0, 1, tzinfo=UTC),
    )

    assert first is not None
    assert second is not None
    assert first.receipt.id == second.receipt.id
    assert first.receipt.evidence_type == expected_type
    assert first.receipt.details["content_sha256"] != second.receipt.details["content_sha256"]

    next_epoch = build_linked_diff_evidence(
        replace(linked_task, validation_epoch=1),
        session_id="00000000-0000-0000-0000-000000000001",
        validation_context="final state two",
        observed_at=datetime(2026, 7, 25, 0, 2, tzinfo=UTC),
    )
    assert next_epoch is not None
    assert next_epoch.receipt.id != first.receipt.id
