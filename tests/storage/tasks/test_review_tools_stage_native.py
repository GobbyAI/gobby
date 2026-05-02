"""Phase 2 red contracts for stage-native review tools."""

from __future__ import annotations

import pytest

from gobby.storage.tasks._stage_states import (
    IllegalStageTransitionError,
    NoCurrentStageError,
    StageStatesManager,
)
from gobby.storage.tasks._transitions import (
    mark_task_needs_review,
    mark_task_review_approved,
    mark_task_review_rejected,
)
from tests.phase2_stage_contract_helpers import register_contract_tests
from tests.storage.tasks._stage_test_helpers import (
    create_task,
    initialize_manifest,
    set_stage_state,
    spec,
    stage_row,
    stage_rows,
)

pytestmark = pytest.mark.unit


def _planning_task(temp_db, sample_project, *, state: str = "in_progress"):
    task = create_task(temp_db, sample_project, task_type="feature")
    initialize_manifest(temp_db, task.id, [spec("planning", 1), spec("development", 2)])
    set_stage_state(temp_db, task.id, "planning", state)
    return task


def test_needs_review_submits_for_review_on_same_row(temp_db, sample_project) -> None:
    task = _planning_task(temp_db, sample_project, state="in_progress")

    updated = mark_task_needs_review(temp_db, task.id, review_notes="ready")

    assert updated.status == "open"
    assert stage_row(temp_db, task.id, "planning")["state"] == "needs_review"
    assert stage_row(temp_db, task.id, "development")["state"] == "ready"


def test_approved_advances_to_review_approved_on_same_row(temp_db, sample_project) -> None:
    task = _planning_task(temp_db, sample_project, state="needs_review")

    updated = mark_task_review_approved(temp_db, task.id, approval_notes="approved")

    assert updated.status == "open"
    assert stage_row(temp_db, task.id, "planning")["state"] == "review_approved"


def test_approved_does_not_advance_to_next_stage(temp_db, sample_project) -> None:
    task = _planning_task(temp_db, sample_project, state="needs_review")

    mark_task_review_approved(temp_db, task.id)

    rows = stage_rows(temp_db, task.id)
    assert [(row["stage_name"], row["state"]) for row in rows] == [
        ("planning", "review_approved"),
        ("development", "ready"),
    ]


def test_rejected_returns_to_ready_increments_review_rounds(temp_db, sample_project) -> None:
    task = _planning_task(temp_db, sample_project, state="needs_review")

    mark_task_review_rejected(temp_db, task.id, rejection_notes="missing acceptance criteria")

    row = stage_row(temp_db, task.id, "planning")
    assert row["state"] == "ready"
    assert row["review_round_count"] == 1


def test_rejected_does_not_increment_work_attempts(temp_db, sample_project) -> None:
    task = _planning_task(temp_db, sample_project, state="needs_review")

    mark_task_review_rejected(temp_db, task.id, rejection_notes="redo")

    row = stage_row(temp_db, task.id, "planning")
    assert row["work_attempt_count"] == 0
    assert row["review_round_count"] == 1


def test_rejected_over_cap_escalates(temp_db, sample_project) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    initialize_manifest(temp_db, task.id, [spec("planning", 1, max_review_rounds=1)])
    set_stage_state(temp_db, task.id, "planning", "needs_review")

    updated = mark_task_review_rejected(temp_db, task.id, rejection_notes="blocked")

    assert updated.status == "escalated"
    assert stage_row(temp_db, task.id, "planning")["state"] == "ready"


def test_needs_review_rejected_on_policy_none(temp_db, sample_project) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    initialize_manifest(temp_db, task.id, [spec("test_arch", 1)])
    set_stage_state(temp_db, task.id, "test_arch", "in_progress")

    with pytest.raises(IllegalStageTransitionError):
        mark_task_needs_review(temp_db, task.id)


def test_approved_rejected_on_policy_none(temp_db, sample_project) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    initialize_manifest(temp_db, task.id, [spec("test_arch", 1)])
    set_stage_state(temp_db, task.id, "test_arch", "needs_review", review_policy="none")

    with pytest.raises(IllegalStageTransitionError):
        mark_task_review_approved(temp_db, task.id)


def test_no_current_stage_errors(temp_db, sample_project) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")

    for tool in (
        mark_task_needs_review,
        mark_task_review_approved,
        mark_task_review_rejected,
    ):
        with pytest.raises(NoCurrentStageError):
            tool(temp_db, task.id)


def test_wrong_source_state_errors_no_mutation(temp_db, sample_project) -> None:
    task = _planning_task(temp_db, sample_project, state="ready")

    with pytest.raises(IllegalStageTransitionError):
        mark_task_review_approved(temp_db, task.id)

    assert stage_row(temp_db, task.id, "planning")["state"] == "ready"


def test_needs_review_calls_submit_for_review_no_legacy_writes(
    temp_db, sample_project, monkeypatch
) -> None:
    task = _planning_task(temp_db, sample_project, state="in_progress")
    calls: list[str] = []
    original = StageStatesManager.submit_for_review

    def spy(self, task_id, stage_name, **kwargs):
        calls.append(stage_name)
        return original(self, task_id, stage_name, **kwargs)

    monkeypatch.setattr(StageStatesManager, "submit_for_review", spy)

    mark_task_needs_review(temp_db, task.id)

    assert calls == ["planning"]
    assert temp_db.fetchone("SELECT status FROM tasks WHERE id = ?", (task.id,))["status"] == "open"


def test_approved_calls_approve_review_no_legacy_writes(
    temp_db, sample_project, monkeypatch
) -> None:
    task = _planning_task(temp_db, sample_project, state="needs_review")
    calls: list[str] = []
    original = StageStatesManager.approve_review

    def spy(self, task_id, stage_name, **kwargs):
        calls.append(stage_name)
        return original(self, task_id, stage_name, **kwargs)

    monkeypatch.setattr(StageStatesManager, "approve_review", spy)

    mark_task_review_approved(temp_db, task.id)

    assert calls == ["planning"]
    assert temp_db.fetchone("SELECT status FROM tasks WHERE id = ?", (task.id,))["status"] == "open"


def test_rejected_calls_reject_review_no_legacy_writes(
    temp_db, sample_project, monkeypatch
) -> None:
    task = _planning_task(temp_db, sample_project, state="needs_review")
    calls: list[str] = []
    original = StageStatesManager.reject_review

    def spy(self, task_id, stage_name, **kwargs):
        calls.append(stage_name)
        return original(self, task_id, stage_name, **kwargs)

    monkeypatch.setattr(StageStatesManager, "reject_review", spy)

    mark_task_review_rejected(temp_db, task.id, rejection_notes="redo")

    assert calls == ["planning"]
    assert temp_db.fetchone("SELECT status FROM tasks WHERE id = ?", (task.id,))["status"] == "open"

register_contract_tests(
    globals(),
    {
        "test_approved_advances_to_review_approved_on_same_row": (
            "mark_task_review_approved transitions current stage needs_review to review_approved"
        ),
        "test_approved_calls_approve_review_no_legacy_writes": (
            "approval calls StageStatesManager.approve_review without writing legacy statuses"
        ),
        "test_approved_does_not_advance_to_next_stage": (
            "approval leaves next-stage advancement to the dispatcher"
        ),
        "test_approved_rejected_on_policy_none": (
            "approval raises IllegalStageTransitionError for policy-none rows"
        ),
        "test_needs_review_calls_submit_for_review_no_legacy_writes": (
            "needs-review calls submit_for_review without writing legacy statuses"
        ),
        "test_needs_review_rejected_on_policy_none": (
            "needs-review raises IllegalStageTransitionError for policy-none rows"
        ),
        "test_needs_review_submits_for_review_on_same_row": (
            "mark_task_needs_review transitions current stage in_progress to needs_review"
        ),
        "test_no_current_stage_errors": (
            "review tools raise NoCurrentStageError on exhausted manifests"
        ),
        "test_rejected_calls_reject_review_no_legacy_writes": (
            "rejection calls reject_review without writing legacy statuses"
        ),
        "test_rejected_does_not_increment_work_attempts": ("rejection increments no work attempts"),
        "test_rejected_over_cap_escalates": (
            "rejection escalates when review_round_count meets effective cap"
        ),
        "test_rejected_returns_to_ready_increments_review_rounds": (
            "rejection returns same row to ready and increments review_round_count"
        ),
        "test_wrong_source_state_errors_no_mutation": (
            "review tools raise transition errors and mutate nothing from wrong source states"
        ),
    },
    required_symbols=("gobby.storage.tasks._stage_states:StageStatesManager",),
)
