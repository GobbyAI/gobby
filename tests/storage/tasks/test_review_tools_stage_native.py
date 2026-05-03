"""Phase 2 red contracts for stage-native review tools."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._stage_states import (
    IllegalStageTransitionError,
    NoCurrentStageError,
    StageStatesManager,
)
from gobby.storage.tasks._transitions import (
    approve_review,
    reject_review,
    submit_for_review,
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


def _task_columns(temp_db) -> set[str]:
    return {row["name"] for row in temp_db.fetchall("PRAGMA table_info(tasks)")}


def _assert_open_task(updated) -> None:
    assert updated.closed_at is None
    assert updated.escalated_at is None
    assert not updated.is_escalated


def _planning_task(temp_db, sample_project, *, state: str = "in_progress"):
    task = create_task(temp_db, sample_project, task_type="feature")
    initialize_manifest(temp_db, task.id, [spec("planning", 1), spec("development", 2)])
    set_stage_state(temp_db, task.id, "planning", state)
    return task


def test_needs_review_submits_for_review_on_same_row(temp_db, sample_project) -> None:
    task = _planning_task(temp_db, sample_project, state="in_progress")

    updated = submit_for_review(temp_db, task.id, review_notes="ready")

    _assert_open_task(updated)
    assert stage_row(temp_db, task.id, "planning")["state"] == "needs_review"
    assert stage_row(temp_db, task.id, "development")["state"] == "ready"


def test_approved_advances_to_review_approved_on_same_row(temp_db, sample_project) -> None:
    task = _planning_task(temp_db, sample_project, state="needs_review")

    updated = approve_review(temp_db, task.id, approval_notes="approved")

    _assert_open_task(updated)
    assert stage_row(temp_db, task.id, "planning")["state"] == "review_approved"


def test_approved_does_not_advance_to_next_stage(temp_db, sample_project) -> None:
    task = _planning_task(temp_db, sample_project, state="needs_review")

    approve_review(temp_db, task.id)

    rows = stage_rows(temp_db, task.id)
    assert [(row["stage_name"], row["state"]) for row in rows] == [
        ("planning", "review_approved"),
        ("development", "ready"),
    ]


def test_rejected_returns_to_ready_increments_review_rounds(temp_db, sample_project) -> None:
    task = _planning_task(temp_db, sample_project, state="needs_review")

    reject_review(temp_db, task.id, rejection_notes="missing acceptance criteria")

    row = stage_row(temp_db, task.id, "planning")
    assert row["state"] == "ready"
    assert row["review_round_count"] == 1


def test_rejected_does_not_increment_work_attempts(temp_db, sample_project) -> None:
    task = _planning_task(temp_db, sample_project, state="needs_review")

    reject_review(temp_db, task.id, rejection_notes="redo")

    row = stage_row(temp_db, task.id, "planning")
    assert row["work_attempt_count"] == 0
    assert row["review_round_count"] == 1


def test_rejected_over_cap_escalates(temp_db, sample_project) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    initialize_manifest(temp_db, task.id, [spec("planning", 1, max_review_rounds=1)])
    set_stage_state(temp_db, task.id, "planning", "needs_review")

    updated = reject_review(temp_db, task.id, rejection_notes="blocked")

    assert updated.is_escalated
    assert updated.escalated_at is not None
    assert stage_row(temp_db, task.id, "planning")["state"] == "ready"


def test_needs_review_rejected_on_policy_none(temp_db, sample_project) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    initialize_manifest(temp_db, task.id, [spec("test_arch", 1)])
    set_stage_state(temp_db, task.id, "test_arch", "in_progress")

    with pytest.raises(IllegalStageTransitionError):
        submit_for_review(temp_db, task.id)


def test_approved_rejected_on_policy_none(temp_db, sample_project) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    initialize_manifest(temp_db, task.id, [spec("test_arch", 1)])
    set_stage_state(temp_db, task.id, "test_arch", "needs_review", review_policy="none")

    with pytest.raises(IllegalStageTransitionError):
        approve_review(temp_db, task.id)


def test_no_current_stage_errors(temp_db, sample_project) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    temp_db.execute(
        "UPDATE task_stage_states SET state = 'done' WHERE task_id = ?",
        (task.id,),
    )

    for tool in (
        submit_for_review,
        approve_review,
        reject_review,
    ):
        with pytest.raises(NoCurrentStageError):
            tool(temp_db, task.id)


def test_wrong_source_state_errors_no_mutation(temp_db, sample_project) -> None:
    task = _planning_task(temp_db, sample_project, state="ready")

    with pytest.raises(IllegalStageTransitionError):
        approve_review(temp_db, task.id)

    assert stage_row(temp_db, task.id, "planning")["state"] == "ready"


def test_fresh_feature_task_planning_review_path_uses_registry_policy(
    temp_db, sample_project
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Production path feature",
        task_type="feature",
    )
    rows = {row["stage_name"]: row for row in stage_rows(temp_db, task.id)}

    assert [row["position"] for row in rows.values()] == list(range(len(rows)))
    for stage_name in ("planning", "expansion", "development", "pr"):
        assert rows[stage_name]["review_policy"] == "required"
    assert rows["planning"]["reviewer_agent"] == "plan-adversary"
    assert rows["expansion"]["reviewer_agent"] == "expansion-qa"
    assert rows["development"]["reviewer_agent"] == "qa-reviewer"
    assert rows["pr"]["reviewer_agent"] is None

    manager.stage_states.start_stage(task.id, "planning", by_session_id=None)
    updated = manager.submit_for_review(task.id)

    _assert_open_task(updated)
    assert stage_row(temp_db, task.id, "planning")["state"] == "needs_review"


def test_fresh_review_anchor_task_has_planning_review_stage(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Plan review round anchor",
        task_type="review_anchor",
        category="planning",
    )

    rows = stage_rows(temp_db, task.id)

    assert [(row["stage_name"], row["position"]) for row in rows] == [("planning", 0)]
    planning = rows[0]
    assert planning["state"] == "ready"
    assert planning["review_policy"] == "required"
    assert planning["reviewer_agent"] == "plan-adversary"


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

    submit_for_review(temp_db, task.id)

    assert calls == ["planning"]
    assert "status" not in _task_columns(temp_db)


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

    approve_review(temp_db, task.id)

    assert calls == ["planning"]
    assert "status" not in _task_columns(temp_db)


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

    reject_review(temp_db, task.id, rejection_notes="redo")

    assert calls == ["planning"]
    assert "status" not in _task_columns(temp_db)


register_contract_tests(
    globals(),
    {
        "test_approved_advances_to_review_approved_on_same_row": (
            "approve_review transitions current stage needs_review to review_approved"
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
            "submit_for_review transitions current stage in_progress to needs_review"
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
