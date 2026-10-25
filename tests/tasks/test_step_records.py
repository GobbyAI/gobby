"""PostgreSQL tests for role-owned task step records and atomic independent review."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import CheckoutNotFoundError
from gobby.storage.projects import PERSONAL_PROJECT_ID, LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.step_records import (
    StepRecordError,
    StepStatus,
    StepSubmission,
    TaskStep,
    TaskStepRecord,
    approved_task_step,
    list_task_step_records,
    record_task_step,
    task_step_requirements,
)
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

_TIP = "871d706" + "a" * 33
_NEXT = "9c3ff62" + "b" * 33
_LATER = "c0ffee1" + "c" * 33
_FEEDBACK = "Round 1 finding:\n- close skips the record gate.\n" + "detail " * 200
_EXHAUSTED = "Three independent review rejections in the current epoch"


class _Seats:
    """Registered sessions whose installed definitions grant task step writes."""

    def __init__(self, db: HubDatabase, project_id: str) -> None:
        self.db = db
        self.project_id = project_id
        self.next_seq = 1700

    def add(self, definition: str, writes: list[str]) -> str:
        definitions = AgentDefinitionManager(self.db)
        if definitions.get_by_name(definition, self.project_id) is None:
            definitions.create(
                definition,
                {
                    "name": definition,
                    "version": "1.0",
                    "prompts": {"agent": "Record the assigned task step."},
                    "workflows": {"rule_selectors": {"include": []}},
                    "task_step_writes": writes,
                },
                project_id=self.project_id,
            )
        session_id = str(uuid4())
        self.next_seq += 1
        self.db.execute(
            """
            INSERT INTO sessions (
                id, external_id, machine_id, source, project_id, title,
                status, agent_depth, seq_num
            )
            VALUES (%s, %s, %s, 'test', %s, %s, 'active', 0, %s)
            """,
            (
                session_id,
                f"ext-{session_id}",
                require_machine_id(),
                self.project_id,
                definition,
                self.next_seq,
            ),
        )
        SessionVariableManager(self.db).set_variable(session_id, "_agent_type", definition)
        return session_id


@pytest.fixture
def seats(temp_db: HubDatabase, sample_project: dict[str, Any]) -> _Seats:
    return _Seats(temp_db, sample_project["id"])


@pytest.fixture
def team(seats: _Seats) -> dict[str, str]:
    return {
        "developer": seats.add("step-developer", ["test", "implementation"]),
        "reviewer": seats.add("step-reviewer", ["review"]),
        "second_reviewer": seats.add("step-reviewer", ["review"]),
        "closer": seats.add("step-closer", ["closing"]),
    }


@pytest.fixture
def task(temp_db: HubDatabase, sample_project: dict[str, Any], team: dict[str, str]) -> Task:
    manager = LocalTaskManager(temp_db)
    created = manager.create_task(
        sample_project["id"],
        "Step-recorded task",
        validation_criteria="Independent review approves the exact task tip.",
    )
    manager.claim_task(created.id, team["developer"])
    manager.link_commit(created.id, _TIP[:10])
    return manager.get_task(created.id)


def _step(
    db: HubDatabase,
    task: Task,
    session_id: str,
    step: TaskStep,
    status: StepStatus,
    round_number: int = 1,
    *,
    sha: str | None = _TIP,
    epoch: int = 1,
    feedback: str = _FEEDBACK,
) -> tuple[TaskStepRecord, bool]:
    return record_task_step(
        db,
        task_id=task.id,
        session_id=session_id,
        step=step,
        submission=StepSubmission(
            status=status,
            epoch=epoch,
            round_number=round_number,
            feedback=feedback,
            commit_sha=sha,
        ),
    )


def test_review_record_is_atomic_and_idempotent(
    temp_db: HubDatabase, task: Task, team: dict[str, str]
) -> None:
    manager = LocalTaskManager(temp_db)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: _step(temp_db, task, team["reviewer"], "review", "rejected"), range(4)
            )
        )

    assert sorted(created for _, created in results) == [False, False, False, True]
    assert len({record.id for record, _ in results}) == 1
    record = results[0][0]
    assert (record.step, record.status, record.session_id) == (
        "review",
        "rejected",
        team["reviewer"],
    )
    assert (record.agent_definition, record.agent_definition_version) == ("step-reviewer", "1.0")
    assert len(record.agent_definition_hash) == 64
    assert record.feedback == _FEEDBACK
    assert record.created_at <= record.updated_at
    rejected = manager.get_task(task.id)
    assert (rejected.review_status, rejected.review_fail_count) == ("invalid", 1)
    assert rejected.review_feedback == _FEEDBACK
    assert rejected.claimed_by_session_id == team["developer"]
    assert not rejected.is_escalated

    with pytest.raises(StepRecordError, match="cannot be contradicted"):
        _step(temp_db, task, team["reviewer"], "review", "approved")
    with pytest.raises(StepRecordError, match="review round"):
        _step(temp_db, task, team["second_reviewer"], "review", "rejected")

    manager.link_commit(task.id, _NEXT[:10])
    _step(temp_db, task, team["reviewer"], "review", "approved", 2, sha=_NEXT)
    with pytest.raises(StepRecordError, match="cannot be contradicted"):
        _step(temp_db, task, team["second_reviewer"], "review", "rejected", 2, sha=_NEXT)

    approved = manager.get_task(task.id)
    assert (approved.review_status, approved.review_fail_count) == ("valid", 1)
    assert [entry.status for entry in list_task_step_records(temp_db, task.id)] == [
        "rejected",
        "approved",
    ]


def test_independent_exact_sha_review(
    temp_db: HubDatabase, seats: _Seats, task: Task, team: dict[str, str]
) -> None:
    manager = LocalTaskManager(temp_db)
    implementer = seats.add("step-fixer", ["implementation", "review"])
    claimant = seats.add("step-fixer", ["implementation", "review"])
    editor = seats.add("step-reviewer", ["review"])
    SessionVariableManager(temp_db).set_variable(
        editor, "task_edited_files", {task.id: ["src/gobby/tasks/step_records.py"]}
    )
    _step(temp_db, task, implementer, "implementation", "approved", feedback="Implemented.")
    manager.claim_task(task.id, claimant, expected_owner=team["developer"])

    with pytest.raises(StepRecordError, match="claimant"):
        _step(temp_db, task, claimant, "review", "approved")
    manager.claim_task(task.id, team["developer"], expected_owner=claimant)
    with pytest.raises(StepRecordError, match="claimant"):
        _step(temp_db, task, claimant, "review", "approved")
    with pytest.raises(StepRecordError, match="implementer"):
        _step(temp_db, task, implementer, "review", "approved")
    with pytest.raises(StepRecordError, match="task-attributed edits"):
        _step(temp_db, task, editor, "review", "approved")
    with pytest.raises(StepRecordError, match="cannot write the review step"):
        _step(temp_db, task, team["closer"], "review", "approved")

    manager.link_commit(task.id, _NEXT[:10])
    with pytest.raises(StepRecordError, match="latest linked commit"):
        _step(temp_db, task, team["reviewer"], "review", "approved")
    with pytest.raises(StepRecordError, match="must be linked"):
        _step(temp_db, task, team["reviewer"], "review", "approved", sha="f" * 40)
    assert list_task_step_records(temp_db, task.id)[-1].step == "implementation"

    review, created = _step(temp_db, task, team["reviewer"], "review", "approved", sha=_NEXT)
    assert created
    current = manager.get_task(task.id)
    records = list_task_step_records(temp_db, task.id)
    assert approved_task_step(current, records, "review") == review
    assert approved_task_step(current, records, "implementation") is None

    manager.link_commit(task.id, _LATER[:10])
    assert approved_task_step(manager.get_task(task.id), records, "review") is None


def test_three_rejections_and_audited_reset(
    temp_db: HubDatabase, task: Task, team: dict[str, str]
) -> None:
    manager = LocalTaskManager(temp_db)
    variables = SessionVariableManager(temp_db)
    reviewer = team["reviewer"]

    manager.escalate_task(task.id, "Handed off: awaiting landing")
    _step(temp_db, task, reviewer, "review", "in_progress", feedback="Review tool crashed.")
    waited = manager.de_escalate_task(task.id, "Landing complete")
    assert not waited.is_escalated and waited.escalated_at is None
    assert (waited.review_epoch, waited.review_fail_count, waited.review_status) == (
        1,
        0,
        "pending",
    )

    manager.claim_task(task.id, team["developer"])
    for round_number in (1, 2):
        _step(temp_db, task, reviewer, "review", "rejected", round_number)
        assert manager.get_task(task.id).claimed_by_session_id == team["developer"]
    _step(temp_db, task, reviewer, "review", "in_progress", 3, feedback="Sandbox lost; no verdict.")
    assert manager.get_task(task.id).review_fail_count == 2
    _step(temp_db, task, reviewer, "review", "rejected", 3)

    exhausted = manager.get_task(task.id)
    assert exhausted.is_escalated and exhausted.escalated_at is not None
    assert exhausted.escalation_reason == _EXHAUSTED
    assert exhausted.claimed_by_session_id is None
    assert (exhausted.review_status, exhausted.review_fail_count) == ("invalid", 3)
    assert task.id not in (variables.get_variables(team["developer"]).get("claimed_tasks") or {})
    with pytest.raises(StepRecordError, match="audited reset"):
        _step(temp_db, task, reviewer, "review", "rejected", 4)
    with pytest.raises(StepRecordError, match="audited reset"):
        _step(temp_db, task, team["developer"], "implementation", "approved")

    reset = manager.de_escalate_task(task.id, "Triage: scope clarified")
    assert not reset.is_escalated
    assert (reset.review_epoch, reset.review_fail_count, reset.review_status) == (2, 0, "pending")
    assert "De-escalated: Triage: scope clarified (review epoch 2)" in (reset.description or "")
    history = list_task_step_records(temp_db, task.id)
    rejected = [(item.epoch, item.round_number) for item in history if item.status == "rejected"]
    assert rejected == [(1, 1), (1, 2), (1, 3)]
    with pytest.raises(StepRecordError, match="epoch is stale"):
        _step(temp_db, task, reviewer, "review", "approved", 4)
    _approved, created = _step(temp_db, task, reviewer, "review", "approved", epoch=2)
    assert created
    assert manager.get_task(task.id).review_status == "valid"


@pytest.mark.parametrize("authority", ["triage", "operator"])
async def test_only_triage_or_the_operator_resets_an_exhausted_review(
    temp_db: HubDatabase, task: Task, team: dict[str, str], seats: _Seats, authority: str
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    for round_number in (1, 2, 3):
        _step(temp_db, task, team["reviewer"], "review", "rejected", round_number)
    assert manager.get_task(task.id).claimed_by_session_id is None

    async def de_escalate(session_id: str) -> dict[str, Any]:
        with session_context_for_test(session_id):
            result: dict[str, Any] = await registry.call(
                "de_escalate_task", {"task_id": task.id, "reason": "Try another round"}
            )
        return result

    # The exhausted task is unclaimed, so no claim stands between a seat and the reset.
    for seat in ("developer", "reviewer", "closer"):
        refused = await de_escalate(team[seat])
        assert refused.get("error_code") == "REVIEW_RESET_FORBIDDEN", (seat, refused)
    held = manager.get_task(task.id)
    assert held.is_escalated
    assert (held.review_epoch, held.review_fail_count) == (1, 3)

    if authority == "triage":
        resetter = seats.add("triage", [])
    else:
        # The operator's own terminal: a root session with no agent run and no seat role.
        resetter = str(uuid4())
        temp_db.execute(
            """
            INSERT INTO sessions (id, external_id, machine_id, source, project_id, status)
            VALUES (%s, %s, %s, 'claude', %s, 'active')
            """,
            (resetter, f"operator-{resetter}", require_machine_id(), task.project_id),
        )
    reset = await de_escalate(resetter)
    assert "error" not in reset, reset
    after = manager.get_task(task.id)
    assert not after.is_escalated
    assert (after.review_epoch, after.review_fail_count) == (2, 0)


def test_reopen_never_strands_an_exhausted_review(
    temp_db: HubDatabase, task: Task, team: dict[str, str]
) -> None:
    manager = LocalTaskManager(temp_db)
    for round_number in (1, 2, 3):
        _step(temp_db, task, team["reviewer"], "review", "rejected", round_number)

    # Reopen is no reset: clearing the escalation would leave three rejections standing
    # and the task with no path back to review.
    with pytest.raises(ValueError, match="audited reset"):
        manager.reopen_task(task.id, reason="Try again")
    held = manager.get_task(task.id)
    assert held.is_escalated and held.escalation_reason == _EXHAUSTED

    # Closing resolves the escalation; reopening restores it so Triage can still reset it.
    manager.close_task(task.id, reason="wont_do", force=True)
    reopened = manager.reopen_task(task.id, reason="Scope returned")
    assert reopened.closed_at is None
    assert reopened.is_escalated and reopened.escalation_reason == _EXHAUSTED
    assert (reopened.review_epoch, reopened.review_fail_count) == (1, 3)
    reset = manager.de_escalate_task(task.id, "Triage: new approach")
    assert (reset.review_epoch, reset.review_fail_count) == (2, 0)


def test_unreadable_checkout_refuses_close_instead_of_dropping_step_records(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the primary checkout the close policy is unknown, so close fails closed."""
    manager = LocalTaskManager(temp_db)
    work = manager.create_task(
        sample_project["id"],
        "Policy unreadable",
        category="code",
        validation_criteria="Close needs its step records.",
    )

    def unreadable(*_args: object) -> str:
        raise CheckoutNotFoundError("primary checkout is unavailable")

    monkeypatch.setattr("gobby.tasks.step_records.require_root", unreadable)

    with pytest.raises(CheckoutNotFoundError):
        task_step_requirements(temp_db, work)
    with pytest.raises(CheckoutNotFoundError):
        manager.close_task(work.id)
    assert manager.get_task(work.id).closed_at is None


def test_checkout_free_project_closes_under_the_default_policy(temp_db: HubDatabase) -> None:
    """The personal project can never own a checkout, so its tasks use the default policy."""
    LocalProjectManager(temp_db).ensure_exists(PERSONAL_PROJECT_ID, "_personal")
    manager = LocalTaskManager(temp_db)
    chore = manager.create_task(
        PERSONAL_PROJECT_ID, "Personal chore", category="code", validation_criteria="Done."
    )

    assert task_step_requirements(temp_db, chore).required == ()
    manager.close_task(chore.id)
    assert manager.get_task(chore.id).closed_at is not None


def test_round_two_approval_leaves_the_implementation_step_approved(
    temp_db: HubDatabase, task: Task, team: dict[str, str]
) -> None:
    """Round-2 approval records the implementation step; no later write is needed."""
    manager = LocalTaskManager(temp_db)
    _step(temp_db, task, team["developer"], "implementation", "pending")
    _step(temp_db, task, team["reviewer"], "review", "rejected")

    manager.link_commit(task.id, _NEXT[:10])
    _step(temp_db, task, team["reviewer"], "review", "approved", 2, sha=_NEXT)

    approved = manager.get_task(task.id)
    implementation = approved_task_step(
        approved, list_task_step_records(temp_db, task.id), "implementation"
    )
    assert implementation is not None, "round-2 approval left the implementation step pending"
    assert (implementation.status, implementation.commit_sha) == ("approved", _NEXT)
    # Gate 5 reads the same requirements; only the closing record is still outstanding.
    assert task_step_requirements(temp_db, approved).missing == ("closing",)
