"""Approved review step records written through the real role-owned writer."""

from __future__ import annotations

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.step_records import StepSubmission, TaskStepRecord, record_task_step
from gobby.workflows.state_manager import SessionVariableManager

REVIEW_WRITER = "fixture-review-writer"


def grant_review_writes(db: HubDatabase, session_id: str, project_id: str) -> None:
    """Give a registered session an installed definition that writes the review step."""
    definitions = AgentDefinitionManager(db)
    if definitions.get_by_name(REVIEW_WRITER, project_id) is None:
        definitions.create(
            REVIEW_WRITER,
            {
                "name": REVIEW_WRITER,
                "version": "1.0",
                "prompts": {"agent": "Record the assigned review."},
                "workflows": {"rule_selectors": {"include": []}},
                "task_step_writes": ["review"],
            },
            project_id=project_id,
        )
    SessionVariableManager(db).set_variable(session_id, "_agent_type", REVIEW_WRITER)


def approve_review(
    db: HubDatabase, *, task_id: str, reviewer_session_id: str, commit_sha: str
) -> TaskStepRecord:
    """Record a LAND for the task's latest linked commit in its current review round."""
    task = LocalTaskManager(db).get_task(task_id)
    record, _ = record_task_step(
        db,
        task_id=task.id,
        session_id=reviewer_session_id,
        step="review",
        submission=StepSubmission(
            status="approved",
            epoch=task.review_epoch,
            round_number=task.review_fail_count + 1,
            feedback="LAND: reviewed against the integration head.",
            commit_sha=commit_sha,
        ),
    )
    return record
