"""Terminal agent-delivery projection for persisted task-close reviews."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from gobby.autonomous.progress_tracker import ProgressTracker, ProgressType
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_close_reviews import (
    VALIDATOR_RUN_ENDED_SUCCESS_ERROR,
    TaskCloseReviewErrorClass,
    TaskCloseReviewStore,
)
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.agentic_close_review import (
    CLOSE_REVIEW_DAEMON_STOP_RETRY_SECONDS,
    CLOSE_REVIEW_RETRY_SECONDS,
    build_terminal_review_payload,
)
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
        if review.status == "finalizing":
            if task is None or not is_task_closed(task):
                return None
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
            if run_status == "success" and not run_error:
                message = VALIDATOR_RUN_ENDED_SUCCESS_ERROR
            elif run_error:
                message = f"Task-close validator run ended with status {run_status}: {run_error}"
            else:
                message = (
                    f"Task-close validator run ended with status {run_status} before finalization."
                )
            error_class, retry_seconds = _run_ended_retry(run)
            payload = build_terminal_review_payload(
                review,
                status="error",
                message=message,
                error_class=error_class,
                retry_seconds=retry_seconds,
            )
            review = (
                store.finish_run_ended(review.id, result_payload=payload, error=message) or review
            )
    if review.result_payload is None:
        return None
    return review.result_payload, str(
        review.result_payload.get("message") or "Background task-close review completed."
    )


def _run_ended_retry(run: AgentRun | None) -> tuple[TaskCloseReviewErrorClass, int]:
    """Classify a validator run that ended without a verdict, and how long to wait.

    A run ending `success` with no error stays `action_required` on purpose:
    the correct action is immediate, and a retry wait would only park the
    caller. Each retryable cause is read from typed provenance the run itself
    recorded, never from a cleanup outcome.
    """
    if run is None:
        return "action_required", CLOSE_REVIEW_RETRY_SECONDS
    if run.terminal_reason == "daemon_stop":
        # The daemon parked this run on its own shutdown, so the cause is known
        # to be a restart rather than anything about the validator, and it is
        # already over by the time the caller reads this payload.
        return "retryable_infrastructure", CLOSE_REVIEW_DAEMON_STOP_RETRY_SECONDS
    retryable = (
        run.status == "timeout"
        or run.terminal_reason == "provider_quota_exhausted"
        or (
            run.terminal_reason == "spawn_rollback"
            and (run.resume_metadata_json or {}).get("spawn_retryable_infrastructure") is True
        )
    )
    error_class: TaskCloseReviewErrorClass = (
        "retryable_infrastructure" if retryable else "action_required"
    )
    return error_class, CLOSE_REVIEW_RETRY_SECONDS


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
    if not store.mark_delivered(review.id):
        return False
    ProgressTracker(db).record_event(
        review.caller_session_id,
        ProgressType.TASK_CLOSE_REVIEW_COMPLETED,
        tool_name="task_close_review_completed",
        details={"review_id": review.id, "status": review.status},
    )
    return True


__all__ = ["mark_terminal_review_delivered", "terminal_review_delivery"]
