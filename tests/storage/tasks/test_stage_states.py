"""Minimal Phase 2 storage contracts for task stage states."""

from __future__ import annotations

import inspect

import pytest

from gobby.storage.tasks import LocalTaskManager
from tests.storage.tasks._stage_test_helpers import (
    lifecycle_events,
    make_task_with_manifest,
    require_stage_state_types,
    set_stage_state,
    spec,
    stage_row,
)

pytestmark = pytest.mark.unit


def test_stage_states_manager_exposes_reads_writes_and_models(temp_db) -> None:
    types = require_stage_state_types()

    assert inspect.signature(types["StageState"]).parameters.keys() >= {
        "task_id",
        "stage_name",
        "position",
        "state",
        "review_policy",
        "reviewer_agent",
        "entered_at",
        "entered_by_session_id",
        "completed_at",
        "completed_by_session_id",
        "completed_commit_sha",
        "work_attempt_count",
        "review_round_count",
        "max_work_attempts",
        "max_review_rounds",
        "artifact_refs",
        "notes",
        "updated_at",
    }
    assert inspect.signature(types["StageManifestSpec"]).parameters.keys() >= {
        "stage_name",
        "position",
        "max_work_attempts",
        "max_review_rounds",
    }

    manager = types["StageStatesManager"](temp_db, LocalTaskManager(temp_db).lifecycle_events)
    for method_name in {
        "list_for_task",
        "get",
        "current_stage",
        "list_tasks_at_stage",
        "initialize_manifest",
        "add_stage",
        "remove_stage",
        "start_stage",
        "submit_for_review",
        "approve_review",
        "reject_review",
        "complete_stage",
        "fail_stage",
    }:
        assert callable(getattr(manager, method_name))


def test_initialize_manifest_mirrors_registry_policy_and_current_stage(
    temp_db,
    sample_project,
) -> None:
    task, manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("development", 0), spec("pr", 1), spec("merge", 2)],
    )

    rows = manager.list_for_task(task.id)

    assert [row.stage_name for row in rows] == ["development", "pr", "merge"]
    assert [row.position for row in rows] == [0, 1, 2]
    assert rows[0].review_policy == "required"
    assert rows[0].reviewer_agent == "qa-reviewer"
    assert rows[0].work_attempt_count == 0
    assert rows[0].review_round_count == 0
    assert rows[0].updated_at is not None
    assert manager.current_stage(task.id).stage_name == "development"


def test_start_stage_increments_work_attempts_only_and_emits_event(
    temp_db,
    sample_project,
) -> None:
    task, manager = make_task_with_manifest(temp_db, sample_project, [spec("development", 0)])

    manager.start_stage(task.id, "development", by_session_id="dev-session")

    row = stage_row(temp_db, task.id, "development")
    assert row["state"] == "in_progress"
    assert row["work_attempt_count"] == 1
    assert row["review_round_count"] == 0
    assert lifecycle_events(temp_db, task.id)[-1] == {
        "from_state": "development:ready",
        "to_state": "development:in_progress",
        "reason": "start_stage",
        "by_actor": "dev-session",
    }


def test_invalid_transition_error_carries_full_payload(temp_db, sample_project) -> None:
    types = require_stage_state_types()
    task, manager = make_task_with_manifest(temp_db, sample_project, [spec("development", 0)])
    set_stage_state(
        temp_db,
        task.id,
        "development",
        "in_progress",
        review_policy="required",
    )

    with pytest.raises(types["IllegalStageTransitionError"]) as exc_info:
        manager.complete_stage(task.id, "development", by_session_id="dev-session")

    err = exc_info.value
    assert err.args == ("development", "in_progress", "complete_stage", "required")
    assert err.stage_name == "development"
    assert err.current_state == "in_progress"
    assert err.attempted_transition == "complete_stage"
    assert err.review_policy == "required"
