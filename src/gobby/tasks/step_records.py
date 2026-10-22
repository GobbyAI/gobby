"""Role-owned task records, serialized with the task they describe.

Task comments own the append-only history. Task review fields are projections of
that history; neither ordinary comments nor caller-supplied identities grant
approval authority.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.session_tasks import claimant_sessions
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import Task
from gobby.utils.datetime import utc_now
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import task_edited_file_set

TaskStep = Literal["test", "implementation", "review", "closing"]
StepStatus = Literal["pending", "in_progress", "approved", "rejected"]
STEP_RECORD_AUTHOR_TYPE = "task_step"
TASK_STEPS: tuple[TaskStep, ...] = ("test", "implementation", "review", "closing")
_TERMINAL = frozenset({"approved", "rejected"})


class StepRecordError(ValueError):
    """A record is unauthorized, stale, or contradicts an existing result."""


class StepSubmission(BaseModel):
    """Caller-owned evidence; identity and chronology are deliberately absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: StepStatus
    epoch: int = Field(gt=0, strict=True)
    round_number: int = Field(gt=0, strict=True)
    feedback: str = Field(min_length=1)
    commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    disposition: Literal["moot", "superseded", "wont_do"] | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if not self.feedback.strip():
            raise ValueError("feedback must describe the result")
        if self.commit_sha and self.disposition:
            raise ValueError("choose commit evidence or a no-work disposition")
        if self.status == "approved" and not (self.commit_sha or self.disposition):
            raise ValueError("approval requires commit evidence or a no-work disposition")
        return self


class TaskStepRecord(StepSubmission):
    id: str
    task_id: str
    step: TaskStep
    sequence: int = Field(gt=0)
    agent_definition: str
    agent_definition_version: str
    agent_definition_hash: str
    session_id: str
    created_at: datetime
    updated_at: datetime


def _records(conn: Transaction, task_id: str) -> list[TaskStepRecord]:
    rows = conn.execute(
        "SELECT id, author, body FROM task_comments WHERE task_id = %s AND author_type = %s",
        (task_id, STEP_RECORD_AUTHOR_TYPE),
    ).fetchall()
    records: list[TaskStepRecord] = []
    for row in rows:
        record = TaskStepRecord.model_validate_json(row["body"])
        if (record.id, record.task_id, record.session_id) != (
            str(row["id"]),
            task_id,
            row["author"],
        ):
            raise StepRecordError("step record identity does not match its stored provenance")
        records.append(record)
    return sorted(records, key=lambda record: record.sequence)


def list_task_step_records(db: HubDatabase, task_id: str) -> list[TaskStepRecord]:
    with db.transaction() as conn:
        return _records(conn, task_id)


def current_task_steps(records: list[TaskStepRecord], epoch: int) -> dict[TaskStep, TaskStepRecord]:
    """Project the latest record per step without discarding historical evidence."""
    return {record.step: record for record in records if record.epoch == epoch}


def _writer(
    db: HubDatabase, task: Task, session_id: str, step: TaskStep
) -> tuple[AgentDefinitionBody, str]:
    session = SessionManager(db).get(session_id)
    if session is None or session.project_id != task.project_id:
        raise StepRecordError("step writer must be a registered session in the task project")
    agent_name = (
        SessionVariableManager(db)
        .get_variable_subset(session_id, ("_agent_type",))
        .get("_agent_type")
    )
    definition = (
        AgentDefinitionManager(db).get_by_name(agent_name, session.project_id)
        if isinstance(agent_name, str) and agent_name
        else None
    )
    if definition is None or not definition.enabled:
        raise StepRecordError("step writer requires an enabled installed agent definition")
    body = AgentDefinitionBody.model_validate(
        {**definition.definition_json, "name": definition.name}
    )
    if step not in body.task_step_writes:
        raise StepRecordError(f"agent definition {body.name} cannot write the {step} step")
    digest = hashlib.sha256(
        json.dumps(definition.definition_json, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body, digest


def _require_independent_reviewer(
    db: HubDatabase,
    conn: Transaction,
    task: Task,
    session_id: str,
    records: list[TaskStepRecord],
) -> None:
    if task.claimed_by_session_id == session_id or session_id in claimant_sessions(conn, task.id):
        raise StepRecordError("a current or former claimant cannot approve its own task")
    if any(
        record.step == "implementation" and record.session_id == session_id for record in records
    ):
        raise StepRecordError("an implementer cannot approve its own task")
    variables = SessionVariableManager(db).get_variables(session_id)
    if task_edited_file_set(variables, task.id):
        raise StepRecordError("a session with task-attributed edits cannot approve its own task")


def record_task_step(
    db: HubDatabase,
    *,
    task_id: str,
    session_id: str,
    step: TaskStep,
    submission: StepSubmission,
) -> tuple[TaskStepRecord, bool]:
    """Append evidence and its review projection in one task-locked transaction.

    The transport verifies commit existence. This boundary independently verifies
    its link to the locked task and the installed writer grant. Exact replays
    return the stored record even after escalation or closure.
    """
    if step not in TASK_STEPS:
        raise StepRecordError("unknown task step")
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = %s FOR UPDATE", (task_id,)).fetchone()
        if row is None:
            raise StepRecordError(f"task {task_id} not found")
        task = Task.from_row(row)
        body, digest = _writer(db, task, session_id, step)
        records = _records(conn, task_id)
        matching = [
            record
            for record in records
            if (
                record.step,
                record.session_id,
                record.epoch,
                record.round_number,
                record.commit_sha,
            )
            == (step, session_id, submission.epoch, submission.round_number, submission.commit_sha)
        ]
        previous = matching[-1] if matching else None
        if previous:
            prior_evidence = StepSubmission.model_validate(
                previous.model_dump(include=set(StepSubmission.model_fields))
            )
            if prior_evidence == submission:
                return previous, False
            if previous.status in _TERMINAL:
                raise StepRecordError("a terminal step result cannot be contradicted")
            if previous.status == "in_progress" and submission.status == "pending":
                raise StepRecordError("step progress cannot move backwards")
        if task.closed_at is not None:
            raise StepRecordError("closed tasks cannot receive new step records")
        if task.is_escalated:
            raise StepRecordError("escalated tasks require an audited reset before another round")
        if submission.epoch != task.review_epoch:
            raise StepRecordError("step record epoch is stale")
        if submission.commit_sha:
            if not any(submission.commit_sha.startswith(sha) for sha in task.commits or []):
                raise StepRecordError("step commit must be linked to the task")
            if step in {"implementation", "review", "closing"} and (
                not task.commits or not submission.commit_sha.startswith(task.commits[-1])
            ):
                raise StepRecordError("step commit must match the task's latest linked commit")
        elif submission.status == "approved" and task.commits:
            raise StepRecordError("linked work requires commit evidence, not a no-work disposition")
        if step == "review":
            rejections = {
                record.round_number
                for record in records
                if record.step == "review"
                and record.epoch == task.review_epoch
                and record.status == "rejected"
            }
            if submission.round_number != len(rejections) + 1:
                raise StepRecordError(
                    "review round must follow the current epoch's rejection count"
                )
            if submission.status == "approved":
                _require_independent_reviewer(db, conn, task, session_id, records)
        now = utc_now()
        record = TaskStepRecord(
            **submission.model_dump(),
            id=str(uuid4()),
            task_id=task_id,
            step=step,
            sequence=records[-1].sequence + 1 if records else 1,
            agent_definition=body.name,
            agent_definition_version=body.version or digest,
            agent_definition_hash=digest,
            session_id=session_id,
            created_at=previous.created_at if previous else now,
            updated_at=now,
        )
        conn.execute(
            "INSERT INTO task_comments (id, task_id, author, author_type, body) "
            "VALUES (%s, %s, %s, %s, %s)",
            (record.id, task_id, session_id, STEP_RECORD_AUTHOR_TYPE, record.model_dump_json()),
        )
        if step == "review":
            rejected = submission.status == "rejected"
            count = len(rejections) + int(rejected)
            review_status = {"approved": "valid", "rejected": "invalid"}.get(
                submission.status, "pending"
            )
            conn.execute(
                "UPDATE tasks SET review_status = %s, review_feedback = %s, review_fail_count = %s, "
                "claimed_by_session_id = CASE WHEN %s THEN NULL ELSE claimed_by_session_id END, "
                "escalated_at = CASE WHEN %s THEN %s ELSE escalated_at END, "
                "escalation_reason = CASE WHEN %s THEN %s ELSE escalation_reason END, "
                "is_escalated = CASE WHEN %s THEN TRUE ELSE is_escalated END, updated_at = %s "
                "WHERE id = %s",
                (
                    review_status,
                    submission.feedback,
                    count,
                    rejected,
                    count >= 3,
                    now,
                    count >= 3,
                    "Three independent review rejections in the current epoch",
                    count >= 3,
                    now,
                    task_id,
                ),
            )
        else:
            # Final close freshness includes every step change, not only reviews.
            conn.execute("UPDATE tasks SET updated_at = %s WHERE id = %s", (now, task_id))
    return record, True
