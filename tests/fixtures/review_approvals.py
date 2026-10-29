"""Approved review step records written through the real role-owned writer."""

from __future__ import annotations

from collections.abc import Sequence

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.step_records import (
    StepStatus,
    StepSubmission,
    TaskStep,
    TaskStepRecord,
    record_task_step,
)
from gobby.workflows.state_manager import SessionVariableManager

REVIEW_WRITER = "fixture-review-writer"


def grant_step_writes(
    db: HubDatabase,
    session_id: str,
    project_id: str,
    name: str,
    steps: Sequence[TaskStep],
    *,
    capabilities: Sequence[str] = (),
) -> None:
    """Run a registered session as an installed definition that writes ``steps``."""
    definitions = AgentDefinitionManager(db)
    if definitions.get_by_name(name, project_id) is None:
        definitions.create(
            name,
            {
                "name": name,
                "version": "1.0",
                "prompts": {"agent": "Record the assigned task steps."},
                "workflows": {"rule_selectors": {"include": []}},
                "task_step_writes": list(steps),
                "capabilities": list(capabilities),
            },
            project_id=project_id,
        )
    SessionVariableManager(db).set_variable(session_id, "_agent_type", name)


def grant_review_writes(db: HubDatabase, session_id: str, project_id: str) -> None:
    """Give a registered session an installed definition that writes the review step."""
    grant_step_writes(db, session_id, project_id, REVIEW_WRITER, ("review",))


def approve_review(
    db: HubDatabase, *, task_id: str, reviewer_session_id: str, commit_sha: str
) -> TaskStepRecord:
    """Record a LAND for the task's latest linked commit in its current review round."""
    return _review(
        db,
        task_id,
        reviewer_session_id,
        commit_sha,
        "approved",
        "LAND: reviewed against the integration head.",
    )


def reject_review(
    db: HubDatabase, *, task_id: str, reviewer_session_id: str, commit_sha: str
) -> TaskStepRecord:
    """Record a BOUNCE for the task's latest linked commit in its current review round."""
    return _review(
        db,
        task_id,
        reviewer_session_id,
        commit_sha,
        "rejected",
        "BOUNCE: a blocking finding remains.",
    )


def _review(
    db: HubDatabase,
    task_id: str,
    reviewer_session_id: str,
    commit_sha: str,
    status: StepStatus,
    feedback: str,
) -> TaskStepRecord:
    task = LocalTaskManager(db).get_task(task_id)
    record, _ = record_task_step(
        db,
        task_id=task.id,
        session_id=reviewer_session_id,
        step="review",
        submission=StepSubmission(
            status=status,
            epoch=task.review_epoch,
            round_number=task.review_fail_count + 1,
            feedback=feedback,
            commit_sha=commit_sha,
        ),
    )
    return record
