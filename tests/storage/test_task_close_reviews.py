"""PostgreSQL storage tests for durable task-close reviews."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

import gobby.storage.task_close_reviews as review_storage
import gobby.tasks.agentic_close_review as review_payloads
from gobby.storage.agents import AgentRunTerminalReason, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import ensure_system_session, system_session_id
from gobby.storage.task_close_reviews import (
    REVIEWER_RUN_ENDED_SUCCESS_ERROR,
    QueuedAgentRunSpec,
    TaskCloseReview,
    TaskCloseReviewStaleTaskError,
    TaskCloseReviewStore,
    TerminalTaskCloseReviewStatus,
)
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.machine_id import require_machine_id


@pytest.fixture(autouse=True)
def _task_row(temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
    ensure_system_session(temp_db)
    with temp_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, project_id, title, validation_criteria, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                _TASK_ID,
                sample_project["id"],
                "Close review task",
                "The close review lifecycle is covered.",
                _TASK_UPDATED_AT,
                _TASK_UPDATED_AT,
            ),
        )
        conn.execute(
            """
            INSERT INTO sessions (
                id, external_id, machine_id, source, project_id, title,
                status, agent_depth
            )
            VALUES (%s, %s, %s, 'test', %s, 'Other close caller', 'active', 0)
            """,
            (_OTHER_SESSION_ID, "test-close-caller", require_machine_id(), sample_project["id"]),
        )


def test_one_active_review_per_task_and_terminal_unlock(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    first, created = store.create_or_get_active(**_intent())
    repeated, repeated_created = store.create_or_get_active(
        **{**_intent(), "review_fingerprint": "different"}
    )

    assert created is True
    assert repeated_created is False
    assert repeated.id == first.id
    assert repeated.review_fingerprint == "review"

    payload = {"event": "task_close_review_completed", "status": "error"}
    terminal = store.finish(first.id, status="error", result_payload=payload, error="failed")
    assert terminal is not None and terminal.status == "error"

    fresh, fresh_created = store.create_or_get_active(**_intent())
    assert fresh_created is True
    assert fresh.id != first.id


def test_concurrent_same_snapshot_reuses_review_after_commit_persistence(
    temp_db: HubDatabase,
) -> None:
    store = TaskCloseReviewStore(temp_db)
    first, created = store.create_or_get_active(**{**_intent(), "commit_shas": ("abc123",)})
    assert created is True

    task_row = temp_db.fetchone(
        """
        SELECT commits @> jsonb_build_array(%s::text) AS has_commit, updated_at
        FROM tasks
        WHERE id = %s
        """,
        ("abc123", _TASK_ID),
    )
    assert task_row is not None
    assert task_row["has_commit"] is True
    assert task_row["updated_at"] == _TASK_UPDATED_AT

    repeated, repeated_created = store.create_or_get_active(
        **{**_intent(), "commit_shas": ("abc123",)}
    )

    assert repeated_created is False
    assert repeated.id == first.id


def test_concurrent_update_before_review_creation_returns_stale(
    temp_db: HubDatabase,
) -> None:
    store = TaskCloseReviewStore(temp_db)
    launch_ready = threading.Event()
    update_committed = threading.Event()
    errors: list[BaseException] = []

    def launch() -> None:
        launch_ready.set()
        assert update_committed.wait(timeout=5)
        try:
            store.create_or_get_active(**_intent())
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=launch)
    thread.start()
    assert launch_ready.wait(timeout=5)
    with temp_db.transaction() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET validation_criteria = %s, updated_at = %s
            WHERE id = %s
            """,
            (
                "Updated while close was evaluating.",
                _TASK_UPDATED_AT + timedelta(seconds=1),
                _TASK_ID,
            ),
        )
    update_committed.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], TaskCloseReviewStaleTaskError)
    assert store.get_active_for_task(_TASK_ID) is None


def test_review_lifecycle_preserves_arguments_payload_and_delivery(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    review, _created = store.create_or_get_active(**_intent())
    review = _promote(store, review)

    running = store.bind_run(review.id, review.agent_run_id or "")
    assert running is not None and running.status == "running"
    assert {key: running.close_arguments[key] for key in _ARGUMENTS} == _ARGUMENTS
    assert running.close_arguments["_review_deadline_at"]
    assert running.diff_sha == "d" * 64
    assert running.test_bodies_sha == "e" * 64
    assert running.stable_facts == {"commit_shas": ["abc123"]}
    assert store.get_by_run(review.agent_run_id or "") == running

    finalizing = store.claim_finalizing(review.id, review.agent_run_id or "")
    assert finalizing is not None and finalizing.status == "finalizing"
    assert store.restore_running(review.id, review.agent_run_id or "", error="malformed") is True
    restored = store.get(review.id)
    assert restored is not None and restored.status == "running"

    finalizing = store.claim_finalizing(review.id, review.agent_run_id or "")
    assert finalizing is not None
    payload = {
        "event": "task_close_review_completed",
        "review_id": review.id,
        "status": "closed",
    }
    terminal = store.finish(review.id, status="closed", result_payload=payload)
    assert terminal is not None and terminal.result_payload == payload
    assert terminal.completed_at is not None
    assert terminal.delivered_at is None
    assert [item.id for item in store.list_reconcilable()] == [review.id]

    assert store.mark_delivered(review.id) is True
    delivered = store.get(review.id)
    assert delivered is not None and delivered.delivered_at is not None
    assert store.list_reconcilable() == []


def test_claim_finalizing_from_run_ended_error(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    review, _created = store.create_or_get_active(**_intent())
    review = _promote(store, review)
    running = store.bind_run(review.id, review.agent_run_id or "")
    assert running is not None
    prior_payload = {"status": "error", "message": REVIEWER_RUN_ENDED_SUCCESS_ERROR}
    errored = store.finish(
        review.id,
        status="error",
        result_payload=prior_payload,
        error=REVIEWER_RUN_ENDED_SUCCESS_ERROR,
    )
    assert errored is not None
    assert store.mark_delivered(review.id) is True

    claimed = store.claim_finalizing(review.id, review.agent_run_id or "")

    assert claimed is not None
    assert claimed.status == "finalizing"
    assert claimed.result_payload is None
    assert claimed.error is None
    assert claimed.completed_at is None
    assert claimed.delivered_at is None


def test_claim_finalizing_does_not_reopen_other_terminal_errors(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    review, _created = store.create_or_get_active(**_intent())
    review = _promote(store, review)
    running = store.bind_run(review.id, review.agent_run_id or "")
    assert running is not None
    errored = store.finish(
        review.id,
        status="error",
        result_payload={"status": "error", "message": "boom"},
        error="boom",
    )
    assert errored is not None

    assert store.claim_finalizing(review.id, review.agent_run_id or "") is None
    current = store.get(review.id)
    assert current is not None and current.status == "error"


def test_late_claim_yields_to_newer_active_review(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    old_review, _created = store.create_or_get_active(**_intent())
    old_review = _promote(store, old_review)
    assert store.bind_run(old_review.id, old_review.agent_run_id or "") is not None
    prior_payload = {"status": "error", "message": REVIEWER_RUN_ENDED_SUCCESS_ERROR}
    assert (
        store.finish(
            old_review.id,
            status="error",
            result_payload=prior_payload,
            error=REVIEWER_RUN_ENDED_SUCCESS_ERROR,
        )
        is not None
    )
    newer, created = store.create_or_get_active(
        **{
            **_intent(),
            "review_fingerprint": "newer-review",
            "evidence_fingerprint": "newer-evidence",
        }
    )
    assert created is True
    newer = _promote(store, newer)
    assert store.bind_run(newer.id, newer.agent_run_id or "") is not None

    assert store.claim_finalizing(old_review.id, old_review.agent_run_id or "") is None

    old_current = store.get(old_review.id)
    active = store.get_active_for_task(old_review.task_id)
    assert old_current is not None
    assert old_current.status == "error"
    assert old_current.result_payload == prior_payload
    assert active is not None
    assert active.id == newer.id
    assert active.status == "running"


def test_run_end_finish_does_not_overwrite_finalizing(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    review, _created = store.create_or_get_active(**_intent())
    review = _promote(store, review)
    running = store.bind_run(review.id, review.agent_run_id or "")
    assert running is not None
    finalizing = store.claim_finalizing(review.id, review.agent_run_id or "")
    assert finalizing is not None

    unchanged = store.finish_run_ended(
        review.id,
        result_payload={"status": "error", "message": "run ended"},
        error="run ended",
    )

    assert unchanged is not None
    assert unchanged.status == "finalizing"
    assert unchanged.result_payload is None
    assert unchanged.error is None


def test_delivered_rejection_is_reused_for_identical_evidence(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    _finish_terminal_review(store, status="invalid")
    newest_delivered = _finish_terminal_review(store, status="invalid")
    _finish_terminal_review(store, status="invalid", delivered=False)

    reused = store.get_delivered_rejected_verdict(
        task_id=_TASK_ID,
        review_fingerprint="review",
        expected_task_updated_at=_TASK_UPDATED_AT,
    )

    assert reused is not None
    assert reused.id == newest_delivered.id
    assert reused.result_payload == newest_delivered.result_payload


def test_changed_review_summary_does_not_reuse_delivered_rejection(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    rejected = _finish_terminal_review(store, status="invalid")

    identical_retry = store.get_delivered_rejected_verdict(
        task_id=_TASK_ID,
        review_fingerprint="review",
        expected_task_updated_at=_TASK_UPDATED_AT,
    )

    reused = store.get_delivered_rejected_verdict(
        task_id=_TASK_ID,
        review_fingerprint="review-with-new-summary",
        expected_task_updated_at=_TASK_UPDATED_AT,
    )

    assert identical_retry is not None and identical_retry.id == rejected.id
    assert reused is None


def test_no_terminal_rejection_returns_no_reusable_verdict(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)

    reused = store.get_delivered_rejected_verdict(
        task_id=_TASK_ID,
        review_fingerprint="review",
        expected_task_updated_at=_TASK_UPDATED_AT,
    )

    assert reused is None


def test_delivered_approval_is_not_reused_as_rejection(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    _finish_terminal_review(store, status="closed")

    reused = store.get_delivered_rejected_verdict(
        task_id=_TASK_ID,
        review_fingerprint="review",
        expected_task_updated_at=_TASK_UPDATED_AT,
    )

    assert reused is None


def test_active_review_takes_precedence_over_delivered_rejection(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    _finish_terminal_review(store, status="invalid")
    active, created = store.create_or_get_active(**_intent())

    reused = store.get_delivered_rejected_verdict(
        task_id=_TASK_ID,
        review_fingerprint="review",
        expected_task_updated_at=_TASK_UPDATED_AT,
    )

    assert created is True
    assert active.status == "queued"
    assert reused is None


def test_task_update_after_evaluation_prevents_rejection_reuse(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    _finish_terminal_review(store, status="invalid")
    with temp_db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET updated_at = %s WHERE id = %s",
            (_TASK_UPDATED_AT + timedelta(seconds=1), _TASK_ID),
        )

    reused = store.get_delivered_rejected_verdict(
        task_id=_TASK_ID,
        review_fingerprint="review",
        expected_task_updated_at=_TASK_UPDATED_AT,
    )

    assert reused is None


def test_unjudged_attempts_count_every_review_that_never_reached_a_verdict(
    temp_db: HubDatabase,
) -> None:
    # The count decides how far along the validator candidate list the next
    # attempt starts, so it must separate "the reviewer never answered" from
    # "the reviewer judged the evidence and said no".
    ensure_system_session(temp_db)
    runs = LocalAgentRunManager(temp_db)
    store = TaskCloseReviewStore(temp_db)

    def attempt(
        *,
        status: TerminalTaskCloseReviewStatus,
        run_failed: bool,
        terminal_reason: AgentRunTerminalReason | None = None,
    ) -> None:
        review, _ = store.create_or_get_active(**_intent())
        review = _promote(store, review)
        run = runs.get(review.agent_run_id or "")
        assert run is not None
        _activate_run(runs, run.id)
        store.bind_run(review.id, run.id)
        if run_failed:
            runs.fail(run.id, error="run ended", terminal_reason=terminal_reason)
        else:
            runs.complete(run.id, result="verdict")
        store.finish(
            review.id,
            status=status,
            result_payload={"event": "task_close_review_completed", "status": status},
            error="ended" if run_failed else None,
        )

    assert store.count_unjudged_attempts(_TASK_ID) == 0

    attempt(status="error", run_failed=True, terminal_reason="provider_quota_exhausted")
    assert store.count_unjudged_attempts(_TASK_ID) == 1

    attempt(status="error", run_failed=True, terminal_reason="provider_error")
    assert store.count_unjudged_attempts(_TASK_ID) == 2

    # A provider that dies before its first turn leaves nothing behind to
    # classify, so the reason stays NULL. That is the case the count exists
    # for; an unclassified end has to move the next attempt on exactly like a
    # named provider error.
    attempt(status="error", run_failed=True, terminal_reason=None)
    assert store.count_unjudged_attempts(_TASK_ID) == 3

    # A reviewer that ran and rejected the close is evidence about the work,
    # not about the runtime — it must not push the next attempt elsewhere.
    attempt(status="invalid", run_failed=False)
    assert store.count_unjudged_attempts(_TASK_ID) == 3

    # Nor does a run we stopped ourselves.
    attempt(status="error", run_failed=True, terminal_reason="user_cancelled")
    assert store.count_unjudged_attempts(_TASK_ID) == 3


_TASK_ID = "00000000-0000-4000-8000-000000000801"
_RUN_ID = "00000000-0000-4000-8000-000000000803"
_OTHER_SESSION_ID = "00000000-0000-4000-8000-000000000804"
_TASK_UPDATED_AT = datetime(2026, 9, 8, tzinfo=UTC)
_ARGUMENTS = {
    "task_id": "#42",
    "reason": "completed",
    "changes_summary": "Implemented.",
    "skip_validation": False,
    "override_justification": None,
    "scope_justification": None,
    "commit_sha": "abc",
    "project_path": "/repo",
    "preview": True,
    "response_detail": "diagnostic",
}


def _intent(*, caller_session_id: str | None = None) -> dict[str, Any]:
    run_id = str(uuid4())
    return {
        "task_id": _TASK_ID,
        "task_ref": "#42",
        "caller_session_id": caller_session_id or system_session_id(),
        "commit_shas": (),
        "close_arguments": _ARGUMENTS,
        "expected_task_updated_at": _TASK_UPDATED_AT,
        "review_fingerprint": "review",
        "evidence_fingerprint": "evidence",
        "diff_sha": "d" * 64,
        "test_bodies_sha": "e" * 64,
        "stable_facts": {"commit_shas": ["abc123"]},
        "review_id": str(uuid4()),
        "run": QueuedAgentRunSpec(
            id=run_id,
            machine_id=require_machine_id(),
            provider="codex",
            model="gpt-test",
            agent_name="task-close-reviewer",
            prompt="Review the task close evidence.",
            timeout_seconds=1200,
            requested_reasoning_effort=None,
        ),
    }


def _finish_terminal_review(
    store: TaskCloseReviewStore,
    *,
    status: TerminalTaskCloseReviewStatus,
    delivered: bool = True,
) -> TaskCloseReview:
    review, created = store.create_or_get_active(**_intent())
    assert created is True
    promoted = _promote(store, review)
    assert promoted.agent_run_id is not None
    running = store.bind_run(promoted.id, promoted.agent_run_id)
    assert running is not None
    payload = {
        "event": "task_close_review_completed",
        "review_id": review.id,
        "status": status,
        "message": "Stored terminal close-review result.",
    }
    terminal = store.finish(review.id, status=status, result_payload=payload)
    assert terminal is not None
    if delivered:
        assert store.mark_delivered(review.id) is True
    result = store.get(review.id)
    assert result is not None
    return result


def _promote(
    store: TaskCloseReviewStore, review: review_storage.TaskCloseReview
) -> review_storage.TaskCloseReview:
    row = store.db.fetchone("SELECT project_id FROM tasks WHERE id = %s", (review.task_id,))
    assert row is not None
    promoted = store.claim_queued(project_id=str(row["project_id"]), max_concurrency=3)
    match = next((item for item in promoted if item.id == review.id), None)
    assert match is not None
    return match


def _activate_run(runs: LocalAgentRunManager, run_id: str) -> None:
    activated = runs.activate_queued(
        run_id,
        child_session_id=system_session_id(),
        provider="codex",
        prompt="Review the task close evidence.",
        workflow_name="task-close-reviewer",
        agent_name="task-close-reviewer",
        model="gpt-test",
        is_local=True,
        requested_reasoning_effort=None,
        effective_reasoning_effort=None,
        reasoning_required=False,
        reasoning_status="not_requested",
        reasoning_message=None,
        timeout_seconds=1200,
        resume_metadata_json=None,
        worktree_id=None,
        clone_id=None,
    )
    assert activated is not None
    assert runs.start(run_id) is not None


def _enqueue_task(store: TaskCloseReviewStore, task: Task) -> TaskCloseReview:
    review_id = str(uuid4())
    run_id = str(uuid4())
    review, created = store.create_or_get_active(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        caller_session_id=system_session_id(),
        commit_shas=(),
        close_arguments={"preview": False, "_review_timeout_seconds": 90},
        expected_task_updated_at=task.updated_at,
        review_fingerprint=f"review-{task.id}",
        evidence_fingerprint=f"evidence-{task.id}",
        diff_sha="d" * 64,
        test_bodies_sha="e" * 64,
        stable_facts={},
        review_id=review_id,
        run=QueuedAgentRunSpec(
            id=run_id,
            machine_id=require_machine_id(),
            provider="codex",
            model="gpt-test",
            agent_name="task-close-reviewer",
            prompt="Review the task close evidence.",
            timeout_seconds=90,
        ),
    )
    assert created is True
    return review


def test_queue_promotes_fifo_with_three_slots_and_project_isolation(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    tasks = LocalTaskManager(temp_db)
    store = TaskCloseReviewStore(temp_db)
    primary_tasks = [
        tasks.create_task(
            str(sample_project["id"]),
            f"Queued close review {index}",
            validation_criteria="The queued close review is exercised.",
        )
        for index in range(5)
    ]
    other_project = LocalProjectManager(temp_db).create(name=f"review-queue-{uuid4()}")
    other_task = tasks.create_task(
        other_project.id,
        "Other project close review",
        validation_criteria="The isolated project queue is exercised.",
    )
    primary_reviews = [_enqueue_task(store, task) for task in primary_tasks]
    other_review = _enqueue_task(store, other_task)

    assert store.list_queued_project_ids() == [str(sample_project["id"]), other_project.id]
    assert all("_review_deadline_at" not in review.close_arguments for review in primary_reviews)

    first_wave = store.claim_queued(
        project_id=str(sample_project["id"]),
        max_concurrency=3,
    )
    assert [review.id for review in first_wave] == [review.id for review in primary_reviews[:3]]
    assert all(review.launched_at is not None for review in first_wave)
    assert all("_review_deadline_at" in review.close_arguments for review in first_wave)
    assert store.claim_queued(project_id=str(sample_project["id"]), max_concurrency=3) == []

    other_wave = store.claim_queued(project_id=other_project.id, max_concurrency=3)
    assert [review.id for review in other_wave] == [other_review.id]

    store.finish(
        first_wave[0].id,
        status="error",
        result_payload={"error": "test slot release"},
        error="test slot release",
    )
    next_wave = store.claim_queued(project_id=str(sample_project["id"]), max_concurrency=3)
    assert [review.id for review in next_wave] == [primary_reviews[3].id]


@pytest.mark.parametrize("elapsed,expected", [(899.999, True), (900, False), (900.001, False)])
def test_infrastructure_wait_survives_reconstruction_until_exact_expiry(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    elapsed: float,
    expected: bool,
) -> None:
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    monkeypatch.setattr(review_payloads, "utc_now", lambda: now)
    store = TaskCloseReviewStore(temp_db)
    review, _ = store.create_or_get_active(**_intent())
    payload = review_payloads.build_terminal_review_payload(
        review,
        status="error",
        error_class="retryable_infrastructure",
    )
    store.finish(review.id, status="error", result_payload=payload)
    assert payload["retry_after"] == "2026-09-13T12:15:00+00:00"
    assert payload["closed"] is False
    monkeypatch.setattr(review_storage, "utc_now", lambda: now + timedelta(seconds=elapsed))
    restarted = TaskCloseReviewStore(temp_db)
    assert restarted.has_retry_wait(_TASK_ID, caller_session_id=system_session_id()) is expected
    assert restarted.has_retry_wait(_TASK_ID, caller_session_id=_OTHER_SESSION_ID) is False
    persisted = restarted.get(review.id)
    assert persisted is not None and persisted.result_payload == payload
    with temp_db.transaction() as conn:
        row = conn.execute("SELECT closed_at FROM tasks WHERE id = %s", (_TASK_ID,)).fetchone()
    assert row is not None and row["closed_at"] is None


@pytest.mark.parametrize("retry", ["entry", "new-launch", "different-caller"])
def test_retry_supersedes_wait_durably(temp_db: HubDatabase, retry: str) -> None:
    store = TaskCloseReviewStore(temp_db)
    review, _ = store.create_or_get_active(**_intent())
    payload = review_payloads.build_terminal_review_payload(
        review,
        status="error",
        error_class="retryable_infrastructure",
    )
    store.finish(review.id, status="error", result_payload=payload)
    assert store.has_retry_wait(_TASK_ID, caller_session_id=system_session_id()) is True
    if retry == "entry":
        store.supersede_retry_wait(_TASK_ID, caller_session_id=system_session_id())
    else:
        intent = _intent()
        if retry == "different-caller":
            intent = _intent(caller_session_id=_OTHER_SESSION_ID)
        newer, created = store.create_or_get_active(**intent)
        assert created is True and newer.active
    restarted = TaskCloseReviewStore(temp_db)
    assert restarted.has_retry_wait(_TASK_ID, caller_session_id=system_session_id()) is False
    previous = restarted.get(review.id)
    assert previous is not None and previous.result_payload == payload
    if retry == "entry":
        assert previous.close_arguments["_retry_superseded_at"]


@pytest.mark.parametrize("status", ["invalid", "stale", "external_pending", "error", "closed"])
def test_noninfrastructure_results_never_grant_wait(
    temp_db: HubDatabase,
    status: TerminalTaskCloseReviewStatus,
) -> None:
    store = TaskCloseReviewStore(temp_db)
    review, _ = store.create_or_get_active(**_intent())
    payload = review_payloads.build_terminal_review_payload(review, status=status)
    store.finish(review.id, status=status, result_payload=payload)
    assert payload["error_class"] == (None if status == "closed" else "action_required")
    assert payload["retry_after"] is None
    assert store.has_retry_wait(_TASK_ID, caller_session_id=system_session_id()) is False


@pytest.mark.parametrize("retry_after", [None, 123, "not-a-date"])
def test_malformed_retry_metadata_fails_closed(temp_db: HubDatabase, retry_after: object) -> None:
    store = TaskCloseReviewStore(temp_db)
    review, _ = store.create_or_get_active(**_intent())
    store.finish(
        review.id,
        status="error",
        result_payload={
            "error_class": "retryable_infrastructure",
            "retry_after": retry_after,
        },
    )
    assert store.has_retry_wait(_TASK_ID, caller_session_id=system_session_id()) is False
