"""PostgreSQL storage tests for durable task-close reviews."""

from __future__ import annotations

from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_close_reviews import TaskCloseReviewStore


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
    }
