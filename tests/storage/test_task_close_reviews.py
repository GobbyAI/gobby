"""PostgreSQL storage tests for durable task-close reviews."""

from __future__ import annotations

from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_close_reviews import (
    VALIDATOR_RUN_ENDED_SUCCESS_ERROR,
    TaskCloseReviewStore,
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


def test_review_lifecycle_preserves_arguments_payload_and_delivery(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    review, _created = store.create_or_get_active(**_intent())

    running = store.bind_run(review.id, _RUN_ID)
    assert running is not None and running.status == "running"
    assert running.close_arguments == _ARGUMENTS
    assert running.diff_sha == "d" * 64
    assert running.test_bodies_sha == "e" * 64
    assert running.stable_facts == {"commit_shas": ["abc123"]}
    assert store.get_by_run(_RUN_ID) == running

    finalizing = store.claim_finalizing(review.id, _RUN_ID)
    assert finalizing is not None and finalizing.status == "finalizing"
    assert store.restore_running(review.id, _RUN_ID, error="malformed") is True
    restored = store.get(review.id)
    assert restored is not None and restored.status == "running"

    finalizing = store.claim_finalizing(review.id, _RUN_ID)
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
    running = store.bind_run(review.id, _RUN_ID)
    assert running is not None
    prior_payload = {"status": "error", "message": VALIDATOR_RUN_ENDED_SUCCESS_ERROR}
    errored = store.finish(
        review.id,
        status="error",
        result_payload=prior_payload,
        error=VALIDATOR_RUN_ENDED_SUCCESS_ERROR,
    )
    assert errored is not None
    assert store.mark_delivered(review.id) is True

    claimed = store.claim_finalizing(review.id, _RUN_ID)

    assert claimed is not None
    assert claimed.status == "finalizing"
    assert claimed.result_payload is None
    assert claimed.error is None
    assert claimed.completed_at is None
    assert claimed.delivered_at is None


def test_claim_finalizing_does_not_reopen_other_terminal_errors(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    review, _created = store.create_or_get_active(**_intent())
    running = store.bind_run(review.id, _RUN_ID)
    assert running is not None
    errored = store.finish(
        review.id,
        status="error",
        result_payload={"status": "error", "message": "boom"},
        error="boom",
    )
    assert errored is not None

    assert store.claim_finalizing(review.id, _RUN_ID) is None
    current = store.get(review.id)
    assert current is not None and current.status == "error"


def test_late_claim_yields_to_newer_active_review(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    old_review, _created = store.create_or_get_active(**_intent())
    assert store.bind_run(old_review.id, _RUN_ID) is not None
    prior_payload = {"status": "error", "message": VALIDATOR_RUN_ENDED_SUCCESS_ERROR}
    assert (
        store.finish(
            old_review.id,
            status="error",
            result_payload=prior_payload,
            error=VALIDATOR_RUN_ENDED_SUCCESS_ERROR,
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
    assert store.bind_run(newer.id, "00000000-0000-4000-8000-000000000099") is not None

    assert store.claim_finalizing(old_review.id, _RUN_ID) is None

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
    running = store.bind_run(review.id, _RUN_ID)
    assert running is not None
    finalizing = store.claim_finalizing(review.id, _RUN_ID)
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


def test_memoized_verdict_is_served_per_evidence_state(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    verdict = {"status": "valid", "criteria": [], "feedback": "Complete."}

    assert (
        store.get_memoized_verdict(
            task_id=_TASK_ID,
            review_fingerprint="review",
            evidence_fingerprint="evidence",
        )
        is None
    )

    store.memoize_verdict(
        task_id=_TASK_ID,
        task_ref="#42",
        caller_session_id=_SESSION_ID,
        close_arguments=_ARGUMENTS,
        review_fingerprint="review",
        evidence_fingerprint="evidence",
        verdict=verdict,
        valid=True,
    )

    assert (
        store.get_memoized_verdict(
            task_id=_TASK_ID,
            review_fingerprint="review",
            evidence_fingerprint="evidence",
        )
        == verdict
    )
    # A new commit or a fresh task-attributed edit moves the evidence
    # fingerprint, which is what makes the exact lookup miss.
    assert (
        store.get_memoized_verdict(
            task_id=_TASK_ID,
            review_fingerprint="review",
            evidence_fingerprint="evidence-after-a-new-commit",
        )
        is None
    )

    later = {"status": "invalid", "criteria": [], "feedback": "Criterion 2 is unmet."}
    store.memoize_verdict(
        task_id=_TASK_ID,
        task_ref="#42",
        caller_session_id=_SESSION_ID,
        close_arguments=_ARGUMENTS,
        review_fingerprint="review",
        evidence_fingerprint="evidence-after-a-new-commit",
        verdict=later,
        valid=False,
    )

    # Prior evidence states remain available only through their exact keys.
    assert (
        store.get_memoized_verdict(
            task_id=_TASK_ID,
            review_fingerprint="review",
            evidence_fingerprint="evidence-after-a-new-commit",
        )
        == later
    )
    assert (
        store.get_memoized_verdict(
            task_id=_TASK_ID,
            review_fingerprint="review",
            evidence_fingerprint="evidence",
        )
        == verdict
    )


def test_memo_rows_stay_out_of_the_agentic_review_lifecycle(temp_db: HubDatabase) -> None:
    store = TaskCloseReviewStore(temp_db)
    store.memoize_verdict(
        task_id=_TASK_ID,
        task_ref="#42",
        caller_session_id=_SESSION_ID,
        close_arguments=_ARGUMENTS,
        review_fingerprint="review",
        evidence_fingerprint="evidence",
        verdict={"status": "invalid", "criteria": [], "feedback": "Criterion 3 is unmet."},
        valid=False,
    )

    # The memo is a completed record, so it must not hold the task's
    # one-active-review lock, and it must never be delivered as a wake.
    assert store.get_active_for_task(_TASK_ID) is None
    assert store.list_reconcilable() == []

    launched, created = store.create_or_get_active(**_intent())
    assert created is True
    assert store.get_active_for_task(_TASK_ID) == launched


_TASK_ID = "00000000-0000-4000-8000-000000000801"
_SESSION_ID = "00000000-0000-4000-8000-000000000802"
_RUN_ID = "00000000-0000-4000-8000-000000000803"
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


def _intent() -> dict[str, Any]:
    return {
        "task_id": _TASK_ID,
        "task_ref": "#42",
        "caller_session_id": _SESSION_ID,
        "close_arguments": _ARGUMENTS,
        "review_fingerprint": "review",
        "evidence_fingerprint": "evidence",
        "diff_sha": "d" * 64,
        "test_bodies_sha": "e" * 64,
        "stable_facts": {"commit_shas": ["abc123"]},
    }
