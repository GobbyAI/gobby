from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import pytest

from gobby.tasks.close_checklist import (
    CloseGateResult,
    evaluate_validation_commands,
    first_failed_gate,
)
from gobby.tasks.transcript_evidence import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)

BASE_TIME = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
EvidenceOutcome = Literal["success", "failure", "unknown"]


def _run(
    order: int,
    *,
    outcome: str = "success",
    categories: tuple[str, ...] = ("test",),
    command: str = "pytest",
) -> TranscriptValidationRun:
    exit_code = 0 if outcome == "success" else 1 if outcome == "failure" else None
    return TranscriptValidationRun(
        session_id="session-1",
        source="codex",
        command=command,
        categories=categories,
        matcher_id="test-matcher",
        label="Test command",
        outcome=cast(EvidenceOutcome, outcome),
        exit_code=exit_code,
        started_at=BASE_TIME + timedelta(seconds=order - 1),
        completed_at=BASE_TIME + timedelta(seconds=order),
        order=order,
    )


def _edit(order: int) -> TranscriptEdit:
    return TranscriptEdit(
        session_id="session-1",
        source="codex",
        path="src/example.py",
        timestamp=BASE_TIME + timedelta(seconds=order),
        order=order,
        tool_name="apply_patch",
    )


@pytest.mark.parametrize("category", ["code", "refactor", "test"])
def test_code_categories_require_clean_test_run(category: str) -> None:
    gate = evaluate_validation_commands(
        task_category=category,
        evidence=TranscriptEvidence(validation_runs=(_run(1),)),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"


@pytest.mark.parametrize("category", ["docs", "planning", "research", "manual"])
def test_non_command_categories_skip_validation(category: str) -> None:
    gate = evaluate_validation_commands(
        task_category=category,
        evidence=TranscriptEvidence(),
        has_attributed_edits=True,
    )

    assert gate.status == "skipped"
    assert gate.details["skip_reason"] == "category"


def test_no_edit_task_skips_validation_for_any_category() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(),
        has_attributed_edits=False,
    )

    assert gate.status == "skipped"
    assert gate.details["skip_reason"] == "no-edit"


def test_config_accepts_any_clean_validation_command() -> None:
    gate = evaluate_validation_commands(
        task_category="config",
        evidence=TranscriptEvidence(
            validation_runs=(_run(1, categories=("lint",), command="ruff check"),)
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"


def test_latest_definitive_result_per_category_cures_failure() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _run(1, outcome="failure"),
                _run(2, outcome="success"),
            )
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"
    assert gate.details["latest_outcomes"] == {"test": "success"}


def test_unresolved_failure_blocks_even_when_required_category_passed() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _run(1, categories=("test",)),
                _run(2, outcome="failure", categories=("lint",), command="ruff check"),
            )
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "failed"
    assert gate.details["unresolved_failure_categories"] == ["lint"]
    assert "Re-run each category clean" in gate.message


def test_edit_after_clean_run_makes_validation_stale() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_run(1),),
            edits=(_edit(2),),
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "failed"
    assert gate.details["fresh_run_count"] == 0
    assert "after the final task edit" in gate.message


def test_commit_after_clean_run_is_neutral() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1),)),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"


def test_clean_run_after_edit_restores_freshness() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_run(1), _run(3)),
            edits=(_edit(2),),
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"


def test_cross_session_evidence_uses_global_ordering() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_run(1), replace(_run(3), session_id="session-2")),
            edits=(replace(_edit(2), session_id="session-2"),),
            sessions=("session-1", "session-2"),
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"
    assert gate.details["sessions"] == ["session-1", "session-2"]


def test_unknown_outcome_never_satisfies_or_blocks_and_names_cure() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_run(1, outcome="unknown"),),
            degraded_capabilities=("codex outcome omitted an exit code",),
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "failed"
    assert gate.details["unresolved_failure_categories"] == []
    assert "unknown results neither satisfy nor block" in gate.message
    assert "definitive exit status" in gate.message


def test_first_failed_gate_stops_ordered_checklist() -> None:
    checklist = first_failed_gate(
        (
            CloseGateResult(1, "task", "passed", "exists"),
            CloseGateResult(2, "session", "failed", "missing"),
            CloseGateResult(3, "repo", "passed", "exists"),
        )
    )

    assert checklist.ready is False
    assert [gate.item for gate in checklist.gates] == [1, 2]
    assert checklist.first_failure is not None
    assert checklist.first_failure.name == "session"
