"""Bounds for the epic_qa cited retry-neutral dispatch cycle (gobby-#17668).

A deterministic-persistent epic integration-workspace failure (e.g. an
unresolvable merge conflict between closed children) surfaces as a
``dispatch_spawn_failed:`` epic_qa fail_stage that cites every closed
automated child. That path is intentionally retry-neutral: it decrements the
epic work_attempt_count and reopens the cited children so the epic can be
retried once the workspace is buildable again.

These tests pin the persistent bound that stops the retry-neutral path from
looping forever:

- ``retry_neutral_failure_count`` accumulates on the epic_qa stage row and
  is never reset or decremented by the retry-neutral path, so a persistent
  failure escalates the epic at the cap.
- A single transient failure still retries without escalating or burning the
  epic work_attempt_count.
- The cited spawn-failure reset preserves child escalation (a human was asked to
  look at those tasks), while a genuine reviewer citation still clears it.
"""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._stage_state_transitions import MAX_EPIC_WORKSPACE_FAILURES
from tests.storage.tasks._stage_test_helpers import (
    initialize_manifest,
    set_stage_state,
    spec,
    stage_row,
    task_row,
)

pytestmark = pytest.mark.unit

_WORKSPACE_FAILURE_REASON = (
    "dispatch_spawn_failed:failed to refresh integration workspace "
    "/tmp/integration from gobby/integration/210-build: CONFLICT"
)
_ESCALATED_AT = "2026-05-07T00:00:00+00:00"


def _epic_with_epic_manifest(manager: LocalTaskManager, sample_project: dict) -> object:
    epic = manager.create_task(
        project_id=sample_project["id"],
        title="Docs epic",
        task_type="epic",
        category="docs",
        validation_criteria="Test task completion is observable.",
    )
    manager.stage_states.initialize_manifest(
        epic.id,
        [spec("development", 0), spec("epic_qa", 1), spec("merge", 2)],
        by_session_id="test",
    )
    return epic


def _closed_child(temp_db, sample_project: dict, parent_id: str, title: str) -> object:
    manager = LocalTaskManager(temp_db)
    leaf = manager.create_task(
        project_id=sample_project["id"],
        title=title,
        parent_task_id=parent_id,
        task_type="task",
        category="docs",
        validation_criteria="Test task completion is observable.",
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
               claimed_by_session_id = NULL
         WHERE id = %s
        """,
        (leaf.id,),
    )
    return leaf


def _escalate_child(temp_db, leaf_id: str, reason: str) -> None:
    temp_db.execute(
        """
        UPDATE tasks
           SET is_escalated = TRUE,
               escalated_at = %s,
               escalation_reason = %s
         WHERE id = %s
        """,
        (_ESCALATED_AT, reason, leaf_id),
    )


def test_persistent_workspace_failure_escalates_epic_after_cap(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    epic = _epic_with_epic_manifest(manager, sample_project)
    leaf = _closed_child(temp_db, sample_project, epic.id, "Leaf")

    for _ in range(MAX_EPIC_WORKSPACE_FAILURES):
        # Each heartbeat: children re-run, development completes, epic runs.
        set_stage_state(temp_db, epic.id, "development", "done", work_attempt_count=1)
        set_stage_state(temp_db, epic.id, "epic_qa", "in_progress", work_attempt_count=1)
        manager.stage_states.fail_stage(
            epic.id,
            "epic_qa",
            reason=_WORKSPACE_FAILURE_REASON,
            by_session_id="dispatcher",
            cited_subtasks=[leaf.id],
        )

    epic_row = task_row(temp_db, epic.id)
    epic_stage = stage_row(temp_db, epic.id, "epic_qa")
    assert epic_stage["retry_neutral_failure_count"] == MAX_EPIC_WORKSPACE_FAILURES
    assert epic_row["is_escalated"] is True
    assert epic_row["escalation_reason"] == "epic_workspace_failed:max"


def test_transient_workspace_failure_does_not_escalate_or_burn_attempts(
    temp_db, sample_project
) -> None:
    manager = LocalTaskManager(temp_db)
    epic = _epic_with_epic_manifest(manager, sample_project)
    leaf = _closed_child(temp_db, sample_project, epic.id, "Leaf")
    set_stage_state(temp_db, epic.id, "development", "done", work_attempt_count=2)
    set_stage_state(temp_db, epic.id, "epic_qa", "in_progress", work_attempt_count=3)

    manager.stage_states.fail_stage(
        epic.id,
        "epic_qa",
        reason=_WORKSPACE_FAILURE_REASON,
        by_session_id="dispatcher",
        cited_subtasks=[leaf.id],
    )

    epic_row = task_row(temp_db, epic.id)
    epic_stage = stage_row(temp_db, epic.id, "epic_qa")
    # One transient failure: counter at 1, well under the cap, attempt-neutral.
    assert epic_stage["retry_neutral_failure_count"] == 1
    assert epic_stage["work_attempt_count"] == 2
    assert epic_row["is_escalated"] is False
    assert stage_row(temp_db, leaf.id, "development")["state"] == "ready"


def test_cited_spawn_failure_preserves_child_escalation(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    epic = _epic_with_epic_manifest(manager, sample_project)
    leaf = _closed_child(temp_db, sample_project, epic.id, "Leaf")
    _escalate_child(temp_db, leaf.id, "development_work_failed:max")
    set_stage_state(temp_db, epic.id, "development", "done", work_attempt_count=1)
    set_stage_state(temp_db, epic.id, "epic_qa", "in_progress", work_attempt_count=1)

    manager.stage_states.fail_stage(
        epic.id,
        "epic_qa",
        reason=_WORKSPACE_FAILURE_REASON,
        by_session_id="dispatcher",
        cited_subtasks=[leaf.id],
    )

    reopened = task_row(temp_db, leaf.id)
    # Reopened for retry, but the human-review escalation is preserved.
    assert reopened["closed_at"] is None
    assert reopened["is_escalated"] is True
    assert reopened["escalation_reason"] == "development_work_failed:max"
    assert reopened["escalated_at"] is not None


def test_genuine_reviewer_citation_clears_child_escalation(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    epic = _epic_with_epic_manifest(manager, sample_project)
    leaf = _closed_child(temp_db, sample_project, epic.id, "Leaf")
    _escalate_child(temp_db, leaf.id, "development_work_failed:max")
    set_stage_state(temp_db, epic.id, "development", "done", work_attempt_count=1)
    set_stage_state(temp_db, epic.id, "epic_qa", "in_progress", work_attempt_count=1)

    # A real reviewer verdict (not a dispatch spawn failure) reopens the child;
    # this is the intended-reset path, so escalation is cleared.
    manager.stage_states.fail_stage(
        epic.id,
        "epic_qa",
        reason="epic_review_findings:child_regressed",
        by_session_id="epic-reviewer",
        cited_subtasks=[leaf.id],
    )

    reopened = task_row(temp_db, leaf.id)
    epic_stage = stage_row(temp_db, epic.id, "epic_qa")
    assert reopened["closed_at"] is None
    assert reopened["is_escalated"] is False
    assert reopened["escalation_reason"] is None
    # Genuine citations are not retry-neutral: no workspace-failure bump.
    assert epic_stage["retry_neutral_failure_count"] == 0
