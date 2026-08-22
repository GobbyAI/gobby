"""Terminal agent-delivery projection for persisted task-close reviews."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_close_reviews import TaskCloseReviewStore
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.agentic_close_review import build_terminal_review_payload
from gobby.tasks.state_semantics import is_task_closed


def terminal_review_delivery(
    db: HubDatabase,
    run_id: str,
) -> tuple[dict[str, Any], str] | None:
    """Return the durable review payload, terminalizing an abandoned active intent."""
    store = TaskCloseReviewStore(db)
    review = store.get_by_run(run_id)
    if review is None:
        return None
    if review.active:
        run = LocalAgentRunManager(db).get(run_id)
        task = LocalTaskManager(db).get_task(review.task_id)
        if review.status == "finalizing" and task is not None and is_task_closed(task):
            close_result = {
                "success": True,
                "can_close": True,
                "closed": True,
                "task_id": task.id,
                "commit_shas": list(task.commits or []),
            }
            payload = build_terminal_review_payload(
                review,
                status="closed",
                close_result=close_result,
                message="Task closed before background-review finalization was interrupted.",
            )
            review = store.finish(review.id, status="closed", result_payload=payload) or review
        else:
            run_status = run.status if run is not None else "missing"
            run_error = run.error if run is not None else None
            message = (
                f"Task-close validator run ended with status {run_status}: {run_error}"
                if run_error
                else f"Task-close validator run ended with status {run_status} before finalization."
            )
            payload = build_terminal_review_payload(review, status="error", message=message)
            review = (
                store.finish(review.id, status="error", result_payload=payload, error=message)
                or review
            )
    if review.result_payload is None:
        return None
    return review.result_payload, str(
        review.result_payload.get("message") or "Background task-close review completed."
    )


def mark_terminal_review_delivered(
    db: HubDatabase,
    payload: Mapping[str, Any],
    delivered_session_ids: Sequence[str],
) -> bool:
    """Mark review delivery only when the originating caller acknowledged its wake."""
    if payload.get("event") != "task_close_review_completed":
        return False
    review_id = payload.get("review_id")
    if not isinstance(review_id, str):
        return False
    store = TaskCloseReviewStore(db)
    review = store.get(review_id)
    if review is None or review.caller_session_id not in delivered_session_ids:
        return False
    return store.mark_delivered(review.id)


__all__ = ["mark_terminal_review_delivered", "terminal_review_delivery"]
