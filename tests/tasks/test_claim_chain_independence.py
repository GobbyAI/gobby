"""Review and landing independence keyed on what a session made, not on the claim it holds.

The claim moves along the team chain (Test Writer, Developer, Reviewer, Dev Manager), so a
claimant can be a legitimate reviewer or closer. A session is barred only when it authored the
task's tests or implementation.
"""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.close_receipts import CloseReceiptError, list_close_receipts, record_close_receipt
from gobby.tasks.step_records import StepRecordError, StepSubmission, record_task_step
from tests.fixtures.landing_case import LandingCase, make_landing_case
from tests.fixtures.review_approvals import approve_review, grant_review_writes, grant_step_writes

pytestmark = pytest.mark.unit


@pytest.fixture
def case(temp_db, sample_project, tmp_path) -> LandingCase:  # type: ignore[no-untyped-def]
    return make_landing_case(temp_db, sample_project, tmp_path)


async def test_reviewer_holding_handed_off_claim_records_review(case: LandingCase) -> None:
    sha = case.candidate("lane", {"docs/change.md": "reviewed by a handed-off claim"})
    task = case.task()
    case.link(task, sha)
    case.implement(task, sha)
    # The developer hands the claim to a reviewer that never made the work.
    LocalTaskManager(case.db).claim_task(task.id, case.reviewer, force=True)
    grant_review_writes(case.db, case.reviewer, case.project_id)

    record = approve_review(
        case.db, task_id=task.id, reviewer_session_id=case.reviewer, commit_sha=sha
    )

    assert record.status == "approved"
    assert record.session_id == case.reviewer


async def test_implementer_holding_claim_cannot_record_review(case: LandingCase) -> None:
    sha = case.candidate("lane", {"docs/change.md": "self review"})
    task = case.task()
    case.link(task, sha)
    case.implement(task, sha)
    grant_review_writes(case.db, case.claimant, case.project_id)

    with pytest.raises(StepRecordError, match="cannot review its own task"):
        approve_review(case.db, task_id=task.id, reviewer_session_id=case.claimant, commit_sha=sha)


async def test_test_writer_cannot_record_review_of_its_own_tests(case: LandingCase) -> None:
    sha = case.candidate("lane", {"tests/test_change.py": "failing tests"})
    task = case.task()
    case.link(task, sha)
    # A Test Writer that wrote the tests and then stopped holding the claim is still their author.
    writer = case.session("test-writer", parent=case.creator)
    grant_step_writes(case.db, writer, case.project_id, "fixture-test-writer", ("test",))
    record_task_step(
        case.db,
        task_id=task.id,
        session_id=writer,
        step="test",
        submission=StepSubmission(
            status="approved",
            epoch=LocalTaskManager(case.db).get_task(task.id).review_epoch,
            round_number=1,
            feedback="RED: the new behavior fails.",
            commit_sha=sha,
        ),
    )
    grant_review_writes(case.db, writer, case.project_id)

    with pytest.raises(StepRecordError, match="cannot review its own task"):
        approve_review(case.db, task_id=task.id, reviewer_session_id=writer, commit_sha=sha)


async def test_implementer_holding_claim_cannot_attest_its_own_landing(case: LandingCase) -> None:
    sha = case.candidate("lane", {"docs/change.md": "self attested"})
    task = case.task()
    case.link(task, sha)
    case.implement(task, sha)

    with pytest.raises(CloseReceiptError, match="cannot attest"):
        record_close_receipt(case.db, task=task, author_session_id=case.claimant, commit_sha=sha)
    assert list_close_receipts(case.db, task.id) == []


async def test_dev_manager_holding_transferred_claim_lands(case: LandingCase) -> None:
    sha = case.candidate("lane", {"docs/change.md": "closed by the handed-off claim"})
    task = case.task()
    case.link(task, sha)
    case.approve(task, sha)
    # The reviewer's approved LAND hands the claim to the Dev Manager for closure.
    manager = case.manager_for(task)
    LocalTaskManager(case.db).claim_task(task.id, manager, force=True)

    landed = await case.land(task, sha, caller=manager)

    assert landed["landed"] is True, landed
    assert case.git("rev-parse", "trunk") == sha
