"""Role-owned task records, serialized with the task they describe.

Task comments own the append-only history. Task review fields are projections of
that history; neither ordinary comments nor caller-supplied identities grant
approval authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator

from gobby.config.tasks import TaskClosePolicyConfig, load_project_task_close_policy
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.project_checkouts import require_root
from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS
from gobby.storage.session_tasks import claimant_sessions
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.storage.tasks._de_escalation import REVIEW_EXHAUSTED, REVIEW_REJECTION_LIMIT
from gobby.storage.tasks._transitions import release_task_claim
from gobby.storage.tasks._updates import update_task
from gobby.tasks.lifecycle_access import (
    CrossProjectTaskForbidden,
    resolve_lifecycle_task,
    task_scope_refusal,
)
from gobby.utils.datetime import utc_now
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.agent_resolver import session_agent_definition
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import task_edited_file_set

TaskStep = Literal["test", "implementation", "review", "closing"]
StepStatus = Literal["pending", "in_progress", "approved", "rejected"]
NoWorkDisposition = Literal["moot", "superseded", "wont_do"]
STEP_RECORD_AUTHOR_TYPE = "task_step"
TASK_STEPS: tuple[TaskStep, ...] = ("test", "implementation", "review", "closing")
_TERMINAL = frozenset({"approved", "rejected"})
# Test evidence may precede implementation; these steps attest the task tip.
_TIP_BOUND: frozenset[TaskStep] = frozenset({"implementation", "review", "closing"})


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
    disposition: NoWorkDisposition | None = None

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
    sequence: int = Field(gt=0, strict=True)
    agent_definition: str
    agent_definition_version: str
    agent_definition_hash: str
    agent_capabilities: list[StrictStr] = Field(default_factory=list)
    # None is an old record with no authority snapshot; it grants no review authority.
    supplemental_review: StrictBool | None = None
    session_id: str
    created_at: datetime
    updated_at: datetime


def _stored_record(row: Mapping[str, Any]) -> TaskStepRecord:
    record = TaskStepRecord.model_validate_json(row["body"])
    if (record.id, record.task_id, record.session_id) != (
        str(row["id"]),
        str(row["task_id"]),
        row["author"],
    ):
        raise StepRecordError("step record identity does not match its stored provenance")
    return record


def _records(conn: Transaction, task_id: str) -> list[TaskStepRecord]:
    rows = conn.execute(
        "SELECT id, task_id, author, body FROM task_comments "
        "WHERE task_id = %s AND author_type = %s",
        (task_id, STEP_RECORD_AUTHOR_TYPE),
    ).fetchall()
    return sorted(map(_stored_record, rows), key=lambda record: record.sequence)


def step_records_by_task(db: HubDatabase) -> dict[str, list[TaskStepRecord]]:
    """Every task's step history in sequence order, read in one query for backups."""
    rows = db.fetchall(
        "SELECT id, task_id, author, body FROM task_comments WHERE author_type = %s",
        (STEP_RECORD_AUTHOR_TYPE,),
    )
    history: dict[str, list[TaskStepRecord]] = {}
    for record in sorted(map(_stored_record, rows), key=lambda record: record.sequence):
        history.setdefault(record.task_id, []).append(record)
    return history


def restore_task_step_records(
    conn: Transaction, task_id: str, history: Sequence[TaskStepRecord]
) -> bool:
    """Append backed-up history the task lacks; refuse history that diverges from storage.

    The reserved author type is written here, never read from the backup, so restored
    comments cannot become step records. Returns False, writing nothing, on divergence.
    """
    conn.execute("SELECT 1 FROM tasks WHERE id = %s FOR UPDATE", (task_id,))
    stored = _records(conn, task_id)
    shared = min(len(stored), len(history))
    if [(r.id, r.sequence) for r in stored[:shared]] != [
        (r.id, r.sequence) for r in history[:shared]
    ]:
        return False
    for record in history[len(stored) :]:
        conn.execute(
            "INSERT INTO task_comments "
            "(id, task_id, author, author_type, body, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                record.id,
                task_id,
                record.session_id,
                STEP_RECORD_AUTHOR_TYPE,
                record.model_dump_json(),
                record.updated_at,
                record.updated_at,
            ),
        )
    return True


def list_task_step_records(db: HubDatabase, task_id: str) -> list[TaskStepRecord]:
    with db.transaction() as conn:
        return _records(conn, task_id)


def current_task_steps(records: list[TaskStepRecord], epoch: int) -> dict[TaskStep, TaskStepRecord]:
    """Project the latest record per step without discarding historical evidence."""
    return {
        record.step: record
        for record in records
        if record.epoch == epoch
        and (record.step != "review" or record.supplemental_review is False)
    }


def approved_task_step(
    task: Task, records: list[TaskStepRecord], step: TaskStep
) -> TaskStepRecord | None:
    """Return the current-epoch approval of one step for the task's present evidence."""
    record = current_task_steps(records, task.review_epoch).get(step)
    if record is None or record.status != "approved":
        return None
    commits = task.commits or []
    if record.commit_sha is None:
        return None if commits else record
    evidence = commits[-1:] if step in _TIP_BOUND else commits
    return record if any(record.commit_sha.startswith(sha) for sha in evidence) else None


def independent_review_approval(
    task: Task, records: list[TaskStepRecord], commit_sha: str, lander_session_id: str
) -> TaskStepRecord | None:
    """The current review approval of exactly ``commit_sha``, written by another session."""
    current = approved_task_step(task, records, "review")
    if current is None or current.commit_sha != commit_sha.strip().lower():
        return None
    return None if current.session_id == lander_session_id else current


def landing_review_approval(
    task: Task, records: list[TaskStepRecord], commit_sha: str, reviewer_session_id: str
) -> TaskStepRecord | None:
    """The reviewer's own approval of exactly the commit the current review approves.

    Every reviewer that approved that commit in the current epoch may land it, so a
    second approver can replay or recover the first one's landing.
    """
    sha = commit_sha.strip().lower()
    current = approved_task_step(task, records, "review")
    if current is None or current.commit_sha != sha:
        return None
    return next(
        (
            record
            for record in records
            if record.step == "review"
            and record.status == "approved"
            and record.epoch == task.review_epoch
            and record.commit_sha == sha
            and record.session_id == reviewer_session_id
        ),
        None,
    )


@dataclass(frozen=True)
class StepRequirements:
    """Steps project policy requires for a close, and those not yet approved."""

    required: tuple[TaskStep, ...]
    missing: tuple[TaskStep, ...]


def required_task_steps(
    policy: TaskClosePolicyConfig, *, task_type: str, category: str | None
) -> tuple[TaskStep, ...]:
    """Select the steps a policy requires; epics never need records."""
    if not policy.enforce_step_records or task_type == "epic":
        return ()
    selected = {"implementation", "closing"}
    if category in policy.test_categories:
        selected.add("test")
    if category in policy.review_categories:
        selected.add("review")
    return tuple(step for step in TASK_STEPS if step in selected)


def _project_close_policy(db: HubDatabase, project_id: str) -> TaskClosePolicyConfig:
    # A checkout-free project such as _personal has no policy file to read.
    if project_id in CHECKOUT_FREE_PROJECT_IDS:
        return TaskClosePolicyConfig()
    # The primary checkout owns the policy, so a candidate worktree cannot relax it.
    # Without that checkout the policy is unknown, and the ValueError refuses the close.
    return load_project_task_close_policy(require_root(db, project_id, require_machine_id()))


def task_step_requirements(
    db: HubDatabase, task: Task, *, closer_definition: AgentDefinitionBody | None = None
) -> StepRequirements:
    """Evaluate project and definition policy at the task's present commits."""
    required = required_task_steps(
        _project_close_policy(db, task.project_id),
        task_type=task.task_type,
        category=task.category,
    )
    review_capabilities = (
        closer_definition.required_review_capabilities if closer_definition else []
    )
    if review_capabilities:
        required = tuple(step for step in TASK_STEPS if step in {*required, "review"})
    if not required:
        return StepRequirements((), ())
    records = list_task_step_records(db, task.id)
    missing = tuple(
        step
        for step in required
        if _closing_approval(task, records, step, review_capabilities) is None
    )
    return StepRequirements(required, missing)


def _closing_approval(
    task: Task, records: list[TaskStepRecord], step: TaskStep, review_capabilities: Sequence[str]
) -> TaskStepRecord | None:
    approval = approved_task_step(task, records, step)
    if step == "review" and approval is not None:
        return approval if set(review_capabilities).issubset(approval.agent_capabilities) else None
    return approval


def _rejected_reviews(records: list[TaskStepRecord], epoch: int) -> list[TaskStepRecord]:
    return [
        record
        for record in records
        if record.step == "review"
        and record.supplemental_review is False
        and record.epoch == epoch
        and record.status == "rejected"
    ]


def fixer_round(records: list[TaskStepRecord], epoch: int) -> bool:
    """The epoch stands at its second rejection, when a rejecting reviewer fixes the work."""
    rejected = {record.round_number for record in _rejected_reviews(records, epoch)}
    return len(rejected) == REVIEW_REJECTION_LIMIT - 1


def _review_fixer(
    task: Task, session_id: str, step: TaskStep, records: list[TaskStepRecord]
) -> bool:
    """The claimant that rejected a review this epoch fixes the work after the second rejection."""
    if step != "implementation" or task.claimed_by_session_id != session_id:
        return False
    return fixer_round(records, task.review_epoch) and any(
        record.session_id == session_id for record in _rejected_reviews(records, task.review_epoch)
    )


def _writer(
    db: HubDatabase, task: Task, session_id: str, step: TaskStep, records: list[TaskStepRecord]
) -> tuple[AgentDefinitionBody, str]:
    sessions = SessionManager(db)
    session = sessions.get(session_id)
    if session is None or session.project_id is None:
        raise StepRecordError("step writer must be a registered session")
    if session.project_id != task.project_id:
        # The same lineage or operator authority that resolves a foreign task ref.
        try:
            resolve_lifecycle_task(
                LocalTaskManager(db),
                task.id,
                project_id=session.project_id,
                caller_session_id=session_id,
                sessions=sessions,
            )
        except (CrossProjectTaskForbidden, TaskNotFoundError) as exc:
            raise StepRecordError("step writer has no authority over the task's project") from exc
    definition = session_agent_definition(db, session)
    if definition is None:
        raise StepRecordError("step writer requires an enabled installed agent definition")
    body, digest = definition
    if step not in body.task_step_writes and not _review_fixer(task, session_id, step, records):
        raise StepRecordError(f"agent definition {body.name} cannot write the {step} step")
    if refusal := task_scope_refusal(task, session_id, body.task_step_scope):
        raise StepRecordError(refusal)
    return body, digest


def _is_running_definition_seat(conn: Transaction, session_id: str, definition: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM agent_runs
        WHERE child_session_id = %s AND agent_name = %s AND status = 'running'
        LIMIT 1
        """,
        (session_id, definition),
    ).fetchone()
    return row is not None


def _require_independent_reviewer(
    db: HubDatabase,
    conn: Transaction,
    task: Task,
    session_id: str,
    records: list[TaskStepRecord],
) -> None:
    if task.claimed_by_session_id == session_id or session_id in claimant_sessions(conn, task.id):
        raise StepRecordError("a current or former claimant cannot review its own task")
    if any(
        record.step == "implementation" and record.session_id == session_id for record in records
    ):
        raise StepRecordError("an implementer cannot review its own task")
    variables = SessionVariableManager(db).get_variables(session_id)
    if task_edited_file_set(variables, task.id):
        raise StepRecordError("a session with task-attributed edits cannot review its own task")


def _refuse_held_team(db: HubDatabase, session_id: str) -> None:
    # Local imports: team_lifecycle and team_review import this module at load time.
    from gobby.agents.team_lifecycle import seat_team
    from gobby.agents.team_review import seat_team_held

    binding = seat_team(db, session_id)
    if binding is not None and seat_team_held(db, binding):
        raise StepRecordError("a held epic team records no new review round")


def _advance_implementation(
    conn: Transaction,
    task: Task,
    records: list[TaskStepRecord],
    review: TaskStepRecord,
    now: datetime,
) -> None:
    """Advance the owner's open implementation step to an approved review's commit, in one write.

    Only a pending or in_progress current-epoch implementation record advances. A prior approval
    of an older commit is not carried to a new tip, so its implementer must re-approve it. The
    derived row keeps the implementer's identity, because the close gate reads that step and the
    reviewer must stay independent.
    """
    current = current_task_steps(records, task.review_epoch).get("implementation")
    if current is None or current.status not in {"pending", "in_progress"}:
        return
    advanced = TaskStepRecord(
        status="approved",
        epoch=review.epoch,
        round_number=review.round_number,
        feedback=f"Advanced by approved review {review.id} of {review.commit_sha}.",
        commit_sha=review.commit_sha,
        id=str(uuid4()),
        task_id=task.id,
        step="implementation",
        sequence=review.sequence + 1,
        agent_definition=current.agent_definition,
        agent_definition_version=current.agent_definition_version,
        agent_definition_hash=current.agent_definition_hash,
        session_id=current.session_id,
        created_at=now,
        updated_at=now,
    )
    conn.execute(
        "INSERT INTO task_comments (id, task_id, author, author_type, body) "
        "VALUES (%s, %s, %s, %s, %s)",
        (
            advanced.id,
            task.id,
            advanced.session_id,
            STEP_RECORD_AUTHOR_TYPE,
            advanced.model_dump_json(),
        ),
    )
    conn.execute("UPDATE tasks SET updated_at = %s WHERE id = %s", (now, task.id))


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
    return the stored record first, even after escalation, closure or the end of
    a fixer's claim.
    """
    if step not in TASK_STEPS:
        raise StepRecordError("unknown task step")
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = %s FOR UPDATE", (task_id,)).fetchone()
        if row is None:
            raise StepRecordError(f"task {task_id} not found")
        task = Task.from_row(row)
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
        # Replays return before this check: the fixer's grant ends with its claim.
        body, digest = _writer(db, task, session_id, step, records)
        supplemental = step == "review" and body.supplemental_review
        if task.closed_at is not None:
            raise StepRecordError("closed tasks cannot receive new step records")
        rejections = {
            record.round_number for record in _rejected_reviews(records, task.review_epoch)
        }
        if len(rejections) >= REVIEW_REJECTION_LIMIT:
            raise StepRecordError("exhausted review rounds require an audited reset")
        if submission.epoch != task.review_epoch:
            raise StepRecordError("step record epoch is stale")
        if submission.commit_sha:
            if not any(submission.commit_sha.startswith(sha) for sha in task.commits or []):
                raise StepRecordError("step commit must be linked to the task")
            if step in _TIP_BOUND and (
                not task.commits or not submission.commit_sha.startswith(task.commits[-1])
            ):
                raise StepRecordError("step commit must match the task's latest linked commit")
        elif submission.status == "approved" and task.commits:
            raise StepRecordError("linked work requires commit evidence, not a no-work disposition")
        if step == "review":
            _refuse_held_team(db, session_id)
            if (
                supplemental or "approve_security_review" in body.capabilities
            ) and not _is_running_definition_seat(conn, session_id, body.name):
                raise StepRecordError(
                    "a supplemental or security approval requires a running definition seat"
                )
            if supplemental and submission.status != "approved":
                raise StepRecordError("a supplemental review records only approvals")
            review_key = (submission.epoch, submission.round_number, submission.commit_sha)
            if submission.status in _TERMINAL and any(
                record.step == "review"
                and record.supplemental_review is False
                and (record.epoch, record.round_number, record.commit_sha) == review_key
                and record.status in _TERMINAL
                and record.status != submission.status
                for record in records
            ):
                raise StepRecordError("a terminal review result cannot be contradicted")
            if submission.round_number != len(rejections) + 1:
                raise StepRecordError(
                    "review round must follow the current epoch's rejection count"
                )
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
            agent_capabilities=body.capabilities,
            supplemental_review=body.supplemental_review if step == "review" else None,
            session_id=session_id,
            created_at=previous.created_at if previous else now,
            updated_at=now,
        )
        conn.execute(
            "INSERT INTO task_comments (id, task_id, author, author_type, body) "
            "VALUES (%s, %s, %s, %s, %s)",
            (record.id, task_id, session_id, STEP_RECORD_AUTHOR_TYPE, record.model_dump_json()),
        )
        if step == "review" and not supplemental:
            # Pending and in_progress (including infrastructure errors) never count.
            count = len(rejections) + int(submission.status == "rejected")
            review_status = {"approved": "valid", "rejected": "invalid"}.get(
                submission.status, "pending"
            )
            if count >= REVIEW_REJECTION_LIMIT:
                release_task_claim(
                    db,
                    task_id,
                    review_status=review_status,
                    review_feedback=submission.feedback,
                    review_fail_count=count,
                    escalated_at=now,
                    escalation_reason=REVIEW_EXHAUSTED,
                )
            else:
                update_task(
                    db,
                    task_id,
                    review_status=review_status,
                    review_feedback=submission.feedback,
                    review_fail_count=count,
                )
            if submission.status == "approved" and submission.commit_sha:
                _advance_implementation(conn, task, records, record, now)
        else:
            # Final close freshness includes every step change, not only reviews.
            conn.execute("UPDATE tasks SET updated_at = %s WHERE id = %s", (now, task_id))
    return record, True
