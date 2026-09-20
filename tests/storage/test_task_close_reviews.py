"""PostgreSQL storage tests for durable task-close reviews."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import gobby.storage.task_close_reviews as review_storage
import gobby.tasks.agentic_close_review as review_payloads
from gobby.storage.agents import AgentRunTerminalReason, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import ensure_system_session, system_session_id
from gobby.storage.task_close_reviews import (
    VALIDATOR_RUN_ENDED_SUCCESS_ERROR,
    TaskCloseReviewStaleTaskError,
    TaskCloseReviewStore,
    TerminalTaskCloseReviewStatus,
)


@pytest.fixture(autouse=True)
def _task_row(temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
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


def test_unjudged_attempts_count_every_review_that_never_reached_a_verdict(
    temp_db: HubDatabase,
) -> None:
    # The count decides how far along the validator candidate list the next
    # attempt starts, so it must separate "the validator never answered" from
    # "the validator judged the evidence and said no".
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
        run = runs.create(
            parent_session_id=system_session_id(),
            provider="codex",
            prompt="validate",
        )
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

    # A validator that ran and rejected the close is evidence about the work,
    # not about the runtime — it must not push the next attempt elsewhere.
    attempt(status="invalid", run_failed=False)
    assert store.count_unjudged_attempts(_TASK_ID) == 3

    # Nor does a run we stopped ourselves.
    attempt(status="error", run_failed=True, terminal_reason="user_cancelled")
    assert store.count_unjudged_attempts(_TASK_ID) == 3


_TASK_ID = "00000000-0000-4000-8000-000000000801"
_SESSION_ID = "00000000-0000-4000-8000-000000000802"
_RUN_ID = "00000000-0000-4000-8000-000000000803"
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
    "preview": False,
    "response_detail": "diagnostic",
}


def _intent() -> dict[str, Any]:
    return {
        "task_id": _TASK_ID,
        "task_ref": "#42",
        "caller_session_id": _SESSION_ID,
        "close_arguments": _ARGUMENTS,
        "expected_task_updated_at": _TASK_UPDATED_AT,
        "review_fingerprint": "review",
        "evidence_fingerprint": "evidence",
        "diff_sha": "d" * 64,
        "test_bodies_sha": "e" * 64,
        "stable_facts": {"commit_shas": ["abc123"]},
    }


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
    assert restarted.has_retry_wait(_TASK_ID, caller_session_id=_SESSION_ID) is expected
    assert restarted.has_retry_wait(_TASK_ID, caller_session_id=_RUN_ID) is False
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
    assert store.has_retry_wait(_TASK_ID, caller_session_id=_SESSION_ID) is True
    if retry == "entry":
        store.supersede_retry_wait(_TASK_ID, caller_session_id=_SESSION_ID)
    else:
        intent = _intent()
        if retry == "different-caller":
            intent["caller_session_id"] = _RUN_ID
        newer, created = store.create_or_get_active(**intent)
        assert created is True and newer.active
    restarted = TaskCloseReviewStore(temp_db)
    assert restarted.has_retry_wait(_TASK_ID, caller_session_id=_SESSION_ID) is False
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
    assert store.has_retry_wait(_TASK_ID, caller_session_id=_SESSION_ID) is False


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
    assert store.has_retry_wait(_TASK_ID, caller_session_id=_SESSION_ID) is False
