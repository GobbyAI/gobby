"""Minimal Phase 2 storage contracts for task stage states."""

from __future__ import annotations

import inspect

import pytest

from gobby.storage.tasks import LocalTaskManager
from tests.phase2_stage_contract_helpers import register_contract_tests
from tests.storage.tasks._stage_test_helpers import (
    initialize_manifest,
    lifecycle_events,
    make_task_with_manifest,
    require_stage_state_types,
    set_stage_state,
    spec,
    stage_row,
    task_row,
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


def test_development_manifest_resolves_docs_reviewer_from_selector(
    temp_db,
    sample_project,
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Docs stage manifest task",
        category="docs",
    )
    manager = LocalTaskManager(temp_db).stage_states

    manager.initialize_manifest(
        task.id,
        [spec("development", 0)],
        by_session_id="session-stage-tests",
    )

    row = manager.get(task.id, "development")
    assert row is not None
    assert row.reviewer_agent == "doc-reviewer"


def test_development_manifest_resolves_selector_default_for_non_docs(
    temp_db,
    sample_project,
) -> None:
    task, manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("development", 0)],
        task_type="bug",
    )

    row = manager.get(task.id, "development")
    assert row is not None
    assert row.reviewer_agent == "qa-reviewer"


def test_fixed_registry_reviewer_overrides_selector(temp_db, sample_project) -> None:
    temp_db.execute(
        """
        UPDATE task_stages_registry
           SET reviewer_agent = 'fixed-reviewer'
         WHERE name = 'development'
        """
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Docs fixed reviewer task",
        category="docs",
    )
    manager = LocalTaskManager(temp_db).stage_states

    manager.initialize_manifest(
        task.id,
        [spec("development", 0)],
        by_session_id="session-stage-tests",
    )

    row = manager.get(task.id, "development")
    assert row is not None
    assert row.reviewer_agent == "fixed-reviewer"


def test_selector_category_rules_take_precedence_over_task_type_rules(
    temp_db,
    sample_project,
) -> None:
    temp_db.execute(
        """
        UPDATE task_stages_registry
           SET reviewer_agent_selector_json = ?
         WHERE name = 'development'
        """,
        (
            '{"default": "qa-reviewer", "rules": ['
            '{"task_type": "bug", "reviewer_agent": "bug-reviewer"}, '
            '{"category": "docs", "reviewer_agent": "doc-reviewer"}'
            "]}",
        ),
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Docs bug reviewer task",
        task_type="bug",
        category="docs",
    )
    manager = LocalTaskManager(temp_db).stage_states

    manager.initialize_manifest(
        task.id,
        [spec("development", 0)],
        by_session_id="session-stage-tests",
    )

    row = manager.get(task.id, "development")
    assert row is not None
    assert row.reviewer_agent == "doc-reviewer"


def test_reviewer_selector_snapshot_is_not_retroactive(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Docs reviewer snapshot task",
        category="docs",
    )
    manager = LocalTaskManager(temp_db).stage_states
    manager.initialize_manifest(
        task.id,
        [spec("development", 0)],
        by_session_id="session-stage-tests",
    )

    temp_db.execute(
        """
        UPDATE task_stages_registry
           SET reviewer_agent_selector_json = '{"default": "qa-reviewer", "rules": []}'
         WHERE name = 'development'
        """
    )

    row = manager.get(task.id, "development")
    assert row is not None
    assert row.reviewer_agent == "doc-reviewer"


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
    assert str(err) == (
        "Stage 'development' in state 'in_progress' cannot complete_stage "
        "under review_policy=required"
    )
    assert err.stage_name == "development"
    assert err.current_state == "in_progress"
    assert err.attempted_transition == "complete_stage"
    assert err.review_policy == "required"


def _closed_leaf_for_holistic_failure(temp_db, sample_project, parent_id: str, title: str):
    manager = LocalTaskManager(temp_db)
    leaf = manager.create_task(
        project_id=sample_project["id"],
        title=title,
        parent_task_id=parent_id,
        task_type="task",
        category="docs",
    )
    initialize_manifest(temp_db, leaf.id, [spec("development", 0), spec("merge", 1)])
    set_stage_state(temp_db, leaf.id, "development", "done", work_attempt_count=1)
    set_stage_state(temp_db, leaf.id, "merge", "done", work_attempt_count=1)
    temp_db.execute(
        """
        UPDATE tasks
           SET closed_at = '2026-05-07T00:00:00+00:00',
               closed_reason = 'manifest_exhausted',
               closed_commit_sha = 'abc123',
               claimed_by_session_id = NULL,
               assignee = NULL
         WHERE id = ?
        """,
        (leaf.id,),
    )
    return leaf


def test_holistic_failure_reopens_single_cited_child_to_development(
    temp_db,
    sample_project,
) -> None:
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(
        project_id=sample_project["id"],
        title="Docs epic",
        task_type="epic",
        category="docs",
    )
    manager.stage_states.initialize_manifest(
        parent.id,
        [spec("development", 0), spec("holistic_qa", 1), spec("merge", 2)],
        by_session_id="test",
    )
    set_stage_state(temp_db, parent.id, "development", "done", work_attempt_count=2)
    set_stage_state(temp_db, parent.id, "holistic_qa", "in_progress", work_attempt_count=1)
    leaf = _closed_leaf_for_holistic_failure(temp_db, sample_project, parent.id, "Leaf")

    manager.stage_states.fail_stage(
        parent.id,
        "holistic_qa",
        reason="needs changes",
        by_session_id="holistic-reviewer",
        cited_subtasks=[leaf.id],
    )

    assert stage_row(temp_db, parent.id, "development")["state"] == "ready"
    assert stage_row(temp_db, parent.id, "holistic_qa")["state"] == "ready"
    assert stage_row(temp_db, leaf.id, "development")["state"] == "ready"
    assert stage_row(temp_db, leaf.id, "merge")["state"] == "ready"
    reopened = task_row(temp_db, leaf.id)
    assert reopened["closed_at"] is None
    assert reopened["closed_commit_sha"] is None
    assert reopened["claimed_by_session_id"] is None


def test_holistic_failure_reopens_multiple_cited_children_only(
    temp_db,
    sample_project,
) -> None:
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(
        project_id=sample_project["id"],
        title="Docs epic",
        task_type="epic",
        category="docs",
    )
    manager.stage_states.initialize_manifest(
        parent.id,
        [spec("development", 0), spec("holistic_qa", 1), spec("merge", 2)],
        by_session_id="test",
    )
    set_stage_state(temp_db, parent.id, "development", "done", work_attempt_count=2)
    set_stage_state(temp_db, parent.id, "holistic_qa", "in_progress", work_attempt_count=1)
    first = _closed_leaf_for_holistic_failure(temp_db, sample_project, parent.id, "First")
    second = _closed_leaf_for_holistic_failure(temp_db, sample_project, parent.id, "Second")
    untouched = _closed_leaf_for_holistic_failure(temp_db, sample_project, parent.id, "Untouched")

    manager.stage_states.fail_stage(
        parent.id,
        "holistic_qa",
        reason="needs changes",
        by_session_id="holistic-reviewer",
        cited_subtasks=[first.id, second.id],
    )

    assert task_row(temp_db, first.id)["closed_at"] is None
    assert task_row(temp_db, second.id)["closed_at"] is None
    assert task_row(temp_db, untouched.id)["closed_at"] is not None
    assert stage_row(temp_db, untouched.id, "development")["state"] == "done"


def test_holistic_failure_rejects_cited_non_descendant(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(
        project_id=sample_project["id"],
        title="Docs epic",
        task_type="epic",
        category="docs",
    )
    outsider = manager.create_task(
        project_id=sample_project["id"],
        title="Outsider",
        task_type="task",
        category="docs",
    )
    manager.stage_states.initialize_manifest(
        parent.id,
        [spec("development", 0), spec("holistic_qa", 1), spec("merge", 2)],
        by_session_id="test",
    )
    manager.stage_states.initialize_manifest(
        outsider.id,
        [spec("development", 0)],
        by_session_id="test",
    )
    set_stage_state(temp_db, parent.id, "development", "done")
    set_stage_state(temp_db, parent.id, "holistic_qa", "in_progress", work_attempt_count=1)

    with pytest.raises(ValueError, match="cited_subtasks must be descendants"):
        manager.stage_states.fail_stage(
            parent.id,
            "holistic_qa",
            reason="needs changes",
            by_session_id="holistic-reviewer",
            cited_subtasks=[outsider.id],
        )


register_contract_tests(
    globals(),
    {
        "test_cap_predicate_is_gte_with_inheritance": (
            "effective cap checks use >= and inherit registry defaults at evaluation time"
        ),
        "test_close_failure_escalates_idempotently_on_already_escalated": (
            "terminal close failure escalation is a no-op when the task is already escalated"
        ),
        "test_close_failure_escalates_with_terminal_close_failed_reason": (
            "terminal close failures escalate with terminal_close_failed stage and error reason"
        ),
        "test_close_failure_rolls_back_stage_transition": (
            "terminal close helper failure rolls back the stage transition and task close"
        ),
        "test_close_failure_rolls_back_to_in_progress_for_policy_none_terminal": (
            "policy-none terminal close failure restores in_progress and leaves closed_at null"
        ),
        "test_close_failure_rolls_back_to_in_progress_for_required_policy_terminal_via_validation_override": (
            "required-policy override terminal close failure restores in_progress"
        ),
        "test_close_failure_rolls_back_to_review_approved_for_required_policy_terminal_via_review_path": (
            "required-policy reviewed terminal close failure restores review_approved"
        ),
        "test_close_task_public_api_and_complete_stage_share_helper": (
            "public close_task and terminal complete_stage share _close_task_in_txn"
        ),
        "test_complete_non_terminal_row_does_not_close": (
            "completing a non-terminal manifest row does not close the task"
        ),
        "test_complete_stage_required_policy_rejects_without_override": (
            "complete_stage rejects required-policy in_progress rows without override"
        ),
        "test_complete_terminal_row_closes_task": (
            "completing the highest-position manifest row closes the task atomically"
        ),
        "test_escalated_task_not_re_attempted_by_heartbeat": (
            "terminal-close-failed escalations exclude tasks from automation candidate retries"
        ),
        "test_escalation_helper_db_write_failure_logs_and_reraises": (
            "terminal close escalation helper logs and re-raises DB write failures"
        ),
        "test_escalation_helper_uses_supported_signature_only": (
            "terminal close escalation helper calls escalate_task with the supported signature"
        ),
        "test_fail_does_not_change_either_counter": (
            "fail_stage never increments work_attempt_count or review_round_count"
        ),
        "test_illegal_transition_error_carries_full_payload": (
            "IllegalStageTransitionError exposes stage_name, current_state, attempted_transition, "
            "and review_policy payload fields"
        ),
        "test_invalid_transitions_raise": (
            "every illegal transition matrix row raises IllegalStageTransitionError"
        ),
        "test_merge_terminal_close_via_record_merge_result_uses_same_path": (
            "record_merge_result success delegates terminal close through the same helper"
        ),
        "test_position_uniqueness_enforced": (
            "task_stage_states enforces dense, unique positions per task manifest"
        ),
        "test_reject_review_increments_review_rounds_only": (
            "reject_review increments review_round_count and never work_attempt_count"
        ),
        "test_research_spike_closes_at_prd_done": (
            "research_spike manifests close at the prd terminal row"
        ),
        "test_review_policy_mirrored_at_init_not_retroactive": (
            "registry review_policy edits do not retroactively mutate existing manifest rows"
        ),
        "test_reviewer_agent_mirrored_at_init": (
            "reviewer_agent is mirrored from registry to manifest rows at init time"
        ),
        "test_same_state_cycle_bumps_updated_at": (
            "ready -> in_progress -> ready cycles produce strictly newer updated_at values"
        ),
        "test_start_stage_increments_work_attempts_only": (
            "start_stage is the sole work_attempt_count increment site"
        ),
        "test_transitions_emit_events": (
            "every stage mutator emits task_lifecycle_events with stage:state transitions"
        ),
        "test_updated_at_bumped_on_every_mutator": (
            "every stage mutator bumps StageState.updated_at"
        ),
        "test_validation_override_reason_logged_on_event_row": (
            "validation overrides are logged as validation_override:<reason> lifecycle events"
        ),
    },
    required_symbols=("gobby.storage.tasks._stage_states:StageStatesManager",),
)
