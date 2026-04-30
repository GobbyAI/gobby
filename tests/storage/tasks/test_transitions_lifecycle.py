"""Focused lifecycle transition coverage for review and merge helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._crud import update_task
from gobby.storage.tasks._transitions import (
    POST_BUILD_DESTINATIONS,
    POST_BUILD_TRANSITIONS,
    VALID_STATUSES,
    advance_lifecycle,
    mark_task_merge_failed,
    mark_task_merged,
    mark_task_pr_opened,
    mark_task_review_approved,
    mark_task_review_rejected,
)

pytestmark = pytest.mark.unit


def _project(temp_db, tmp_path):
    return LocalProjectManager(temp_db).create("lifecycle", str(tmp_path))


def _task(
    temp_db,
    tmp_path,
    *,
    task_type: str = "task",
    parent_task_id: str | None = None,
    project_id: str | None = None,
):
    project = _project(temp_db, tmp_path) if project_id is None else None
    return LocalTaskManager(temp_db).create_task(
        project_id or project.id,
        title="transition target",
        task_type=task_type,
        parent_task_id=parent_task_id,
    )


def _set(temp_db, task_id: str, lifecycle: str, status: str) -> None:
    update_task(temp_db, task_id, lifecycle=lifecycle, status=status)


def _artifact(temp_db, task_id: str, column: str):
    row = temp_db.fetchone(f"SELECT {column} FROM task_artifacts WHERE task_id = ?", (task_id,))
    return None if row is None else row[column]


def _event_count(temp_db, task_id: str) -> int:
    row = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM task_lifecycle_events WHERE task_id = ?",
        (task_id,),
    )
    return int(row["count"])


def test_approve_calls_advance_lifecycle(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "pr", "needs_review")

    with patch("gobby.storage.tasks._transitions.advance_lifecycle") as advance:
        advance.return_value = task
        mark_task_review_approved(temp_db, task.id)

    advance.assert_called_once()


def test_reject_calls_advance_lifecycle(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "in_development", "needs_review")

    with patch("gobby.storage.tasks._transitions.advance_lifecycle") as advance:
        advance.return_value = task
        mark_task_review_rejected(temp_db, task.id)

    advance.assert_called_once()


def test_plan_approved_advances_to_test_arch(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "plan_review", "open")
    advance_lifecycle(
        temp_db,
        task.id,
        "plan_review",
        "open",
        {"artifact_updates": {"plan_review_attempts": 2}},
    )

    updated = mark_task_review_approved(temp_db, task.id)

    assert (updated.lifecycle.value, updated.status) == ("test_arch", "open")
    assert _artifact(temp_db, task.id, "plan_review_attempts") == 0


def test_plan_rejected_sets_last_reviewed_hash_and_increments_attempts(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "plan_review", "open")

    updated = mark_task_review_rejected(temp_db, task.id, plan_hash="hash-1")

    assert (updated.lifecycle.value, updated.status) == ("plan_review", "open")
    assert _artifact(temp_db, task.id, "last_reviewed_plan_hash") == "hash-1"
    assert _artifact(temp_db, task.id, "plan_review_attempts") == 1


def test_test_arch_approved_advances(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "test_arch", "open")

    updated = mark_task_review_approved(temp_db, task.id)

    assert (updated.lifecycle.value, updated.status) == ("expanding", "open")


def test_test_arch_rejected_routes_to_plan_review(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "test_arch", "open")

    updated = mark_task_review_rejected(temp_db, task.id)

    assert (updated.lifecycle.value, updated.status) == ("plan_review", "open")
    assert _artifact(temp_db, task.id, "test_arch_attempts") == 1


def test_no_post_build_transition_targets_open_open() -> None:
    assert POST_BUILD_TRANSITIONS
    assert ("open", "open") not in POST_BUILD_DESTINATIONS


def test_expansion_success_preserves_run_id(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "expanding", "open")
    advance_lifecycle(
        temp_db,
        task.id,
        "expanding",
        "open",
        {"artifact_updates": {"expansion_run_id": "run-1"}},
    )

    updated = advance_lifecycle(temp_db, task.id, "in_development", "open")

    assert (updated.lifecycle.value, updated.status) == ("in_development", "open")
    assert _artifact(temp_db, task.id, "expansion_run_id") == "run-1"


def test_expansion_failure_stays_in_expanding_and_increments_attempts(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "expanding", "open")

    updated = advance_lifecycle(
        temp_db,
        task.id,
        "expanding",
        "open",
        {"increment_counters": ("expansion_attempts",), "clear_artifacts": ("expansion_run_id",)},
    )

    assert (updated.lifecycle.value, updated.status) == ("expanding", "open")
    assert _artifact(temp_db, task.id, "expansion_attempts") == 1


def test_dev_complete_transitions_leaf_to_needs_review(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "in_development", "open")

    updated = advance_lifecycle(temp_db, task.id, "in_development", "needs_review")

    assert (updated.lifecycle.value, updated.status) == ("in_development", "needs_review")


def test_qa_approval_parks_leaf(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "in_development", "needs_review")

    updated = mark_task_review_approved(temp_db, task.id)

    assert (updated.lifecycle.value, updated.status) == ("holistic_review", "review_approved")


def test_qa_rejection_resets_leaf(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "in_development", "needs_review")

    updated = mark_task_review_rejected(temp_db, task.id)

    assert (updated.lifecycle.value, updated.status) == ("in_development", "open")
    assert _artifact(temp_db, task.id, "qa_attempts") == 1


def test_epic_advances_to_holistic_when_all_leaves_parked_or_terminal(temp_db, tmp_path) -> None:
    project = _project(temp_db, tmp_path)
    epic = _task(temp_db, tmp_path, task_type="epic", project_id=project.id)
    child = _task(temp_db, tmp_path, parent_task_id=epic.id, project_id=project.id)
    _set(temp_db, epic.id, "in_development", "open")
    _set(temp_db, child.id, "holistic_review", "review_approved")

    updated = advance_lifecycle(temp_db, epic.id, "holistic_review", "open")

    assert (updated.lifecycle.value, updated.status) == ("holistic_review", "open")


def test_holistic_approved_advances_to_pr(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path, task_type="epic")
    _set(temp_db, task.id, "holistic_review", "open")

    updated = mark_task_review_approved(temp_db, task.id)

    assert (updated.lifecycle.value, updated.status) == ("pr", "open")


def test_holistic_rejection_resets_only_cited_leaves(temp_db, tmp_path) -> None:
    project = _project(temp_db, tmp_path)
    epic = _task(temp_db, tmp_path, task_type="epic", project_id=project.id)
    cited = _task(temp_db, tmp_path, parent_task_id=epic.id, project_id=project.id)
    other = _task(temp_db, tmp_path, parent_task_id=epic.id, project_id=project.id)
    _set(temp_db, epic.id, "holistic_review", "open")
    _set(temp_db, cited.id, "holistic_review", "review_approved")
    _set(temp_db, other.id, "holistic_review", "review_approved")

    updated = mark_task_review_rejected(temp_db, epic.id, cited_subtasks=[cited.id])

    assert (updated.lifecycle.value, updated.status) == ("holistic_review", "open")
    assert LocalTaskManager(temp_db).get_task(cited.id).lifecycle.value == "in_development"
    assert LocalTaskManager(temp_db).get_task(other.id).lifecycle.value == "holistic_review"


def test_mark_task_pr_opened_from_open(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "pr", "open")

    updated = mark_task_pr_opened(temp_db, task.id, "https://example.test/pr/1")

    assert (updated.lifecycle.value, updated.status) == ("pr", "needs_review")
    assert _artifact(temp_db, task.id, "pr_url") == "https://example.test/pr/1"


def test_mark_task_pr_opened_from_escalated_clears_escalation(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "pr", "escalated")

    updated = mark_task_pr_opened(temp_db, task.id, "https://example.test/pr/2")

    assert (updated.lifecycle.value, updated.status) == ("pr", "needs_review")
    assert updated.escalated_at is None
    assert updated.escalation_reason is None


def test_pr_external_approval_advances_to_merging(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "pr", "needs_review")

    updated = mark_task_review_approved(temp_db, task.id)

    assert (updated.lifecycle.value, updated.status) == ("merging", "open")


def test_no_transition_produces_non_enum_status() -> None:
    assert "claimed" not in VALID_STATUSES


def test_mark_task_merged_with_pr_url_and_merge_sha_writes_both_artifacts(
    temp_db, tmp_path
) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "merging", "open")

    updated = mark_task_merged(
        temp_db,
        task.id,
        pr_url="https://example.test/pr/3",
        merge_sha="abc123",
    )

    assert (updated.lifecycle.value, updated.status) == ("merged", "closed")
    assert _artifact(temp_db, task.id, "pr_url") == "https://example.test/pr/3"
    assert _artifact(temp_db, task.id, "merge_commit_sha") == "abc123"


def test_mark_task_merged_pr_url_only_leaves_merge_sha_null(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "merging", "open")

    updated = mark_task_merged(temp_db, task.id, pr_url="https://example.test/pr/3")

    assert (updated.lifecycle.value, updated.status) == ("merged", "closed")
    assert _artifact(temp_db, task.id, "pr_url") == "https://example.test/pr/3"
    assert _artifact(temp_db, task.id, "merge_commit_sha") is None


def test_mark_task_merged_no_artifacts_succeeds_for_unattended_path(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "merging", "open")

    updated = mark_task_merged(temp_db, task.id)

    assert (updated.lifecycle.value, updated.status) == ("merged", "closed")
    assert _artifact(temp_db, task.id, "pr_url") is None
    assert _artifact(temp_db, task.id, "merge_commit_sha") is None


def test_mark_task_merged_cascades_close_on_subtree(temp_db, tmp_path) -> None:
    project = _project(temp_db, tmp_path)
    epic = _task(temp_db, tmp_path, task_type="epic", project_id=project.id)
    child = _task(temp_db, tmp_path, parent_task_id=epic.id, project_id=project.id)
    _set(temp_db, epic.id, "merging", "open")
    _set(temp_db, child.id, "holistic_review", "review_approved")

    mark_task_merged(temp_db, epic.id)

    child_after = LocalTaskManager(temp_db).get_task(child.id)
    assert (child_after.lifecycle.value, child_after.status) == ("merged", "closed")


def test_mark_task_merge_failed_unattended_retries(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "merging", "open")

    updated = mark_task_merge_failed(temp_db, task.id, "conflict")

    assert (updated.lifecycle.value, updated.status) == ("merging", "open")
    assert _artifact(temp_db, task.id, "merge_attempts") == 1


def test_mark_task_merge_failed_attended_escalates(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "merging", "open")

    updated = mark_task_merge_failed(temp_db, task.id, "conflict", attended=True)

    assert (updated.lifecycle.value, updated.status) == ("merging", "escalated")
    assert updated.escalation_reason == "merge_failed:conflict"


def test_audit_row_per_transition(temp_db, tmp_path) -> None:
    task = _task(temp_db, tmp_path)
    _set(temp_db, task.id, "pr", "open")

    mark_task_pr_opened(temp_db, task.id, "https://example.test/pr/4")

    assert _event_count(temp_db, task.id) == 1
