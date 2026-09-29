"""Terminal agent-delivery projection for persisted task-close reviews."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from gobby.autonomous.progress_tracker import ProgressTracker, ProgressType
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.agents._sandbox_records import sandbox_record, trusted_live_violation_path
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_close_reviews import (
    REVIEWER_RUN_ENDED_SUCCESS_ERROR,
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

_DENIAL_OPERATION = re.compile(r"(?:^|\s)deny(?:\(\d+\))?\s+([a-z][a-z0-9*-]*)")


def _review_sandbox_denials(run: AgentRun | None) -> dict[str, Any] | None:
    """Summarize enforced denials from the live or retained reviewer log."""
    if run is None:
        return None
    sandbox = sandbox_record(run.resume_metadata_json, include_events=False)
    if sandbox is None or not (count := sandbox["violation_count"]):
        return None
    retained_path = sandbox.get("retained_violation_path")
    raw_sandbox = (run.resume_metadata_json or {}).get("sandbox")
    live_path = (
        trusted_live_violation_path(raw_sandbox.get("violation_path"))
        if isinstance(raw_sandbox, dict)
        else None
    )
    source_path = live_path if live_path is not None and live_path.is_file() else retained_path
    operations: Counter[str] = Counter()
    if source_path:
        try:
            with Path(source_path).open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event_line = event.get("line") if isinstance(event, dict) else None
                    match = (
                        _DENIAL_OPERATION.search(event_line)
                        if isinstance(event_line, str)
                        else None
                    )
                    operations[match.group(1) if match else "unclassified"] += 1
                    if operations.total() >= count:
                        break
        except OSError:
            pass
    if operations.total() < count:
        operations["unclassified"] += count - operations.total()
    return {
        "violation_count": count,
        "operations": dict(sorted(operations.items(), key=lambda item: (-item[1], item[0]))),
        "retained_violation_path": retained_path,
        "retained_settings_path": sandbox.get("retained_settings_path"),
    }


def terminal_review_delivery(
    db: HubDatabase,
    run_id: str,
) -> tuple[dict[str, Any], str] | None:
    """Return the durable review payload, terminalizing an abandoned active intent."""
    store = TaskCloseReviewStore(db)
    review = store.get_by_run(run_id)
    if review is None:
        return None
    run = LocalAgentRunManager(db).get(run_id)
    if review.active:
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
                message = REVIEWER_RUN_ENDED_SUCCESS_ERROR
            elif run_error:
                message = (
                    "Task-close review ended without a verdict "
                    f"(reviewer status {run_status}): {run_error}"
                )
            else:
                message = (
                    "Task-close review ended without a verdict "
                    f"(reviewer status {run_status}) before finalization."
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
    payload = dict(review.result_payload)
    message = str(payload.get("message") or "Background task-close review completed.")
    if sandbox_denials := _review_sandbox_denials(run):
        payload["sandbox_denials"] = sandbox_denials
        breakdown = ", ".join(
            f"{operation} {count}" for operation, count in sandbox_denials["operations"].items()
        )
        count = sandbox_denials["violation_count"]
        noun = "operation" if count == 1 else "operations"
        message += (
            f" SRT denied {count} {noun} ({breakdown})."
            f" Inspect get_agent_result({run_id}) for retained diagnostics."
        )
    return payload, message


def _run_ended_retry(run: AgentRun | None) -> tuple[TaskCloseReviewErrorClass, int]:
    """Classify a reviewer run that ended without a verdict, and how long to wait.

    A run ending `success` with no error stays `action_required` on purpose:
    the correct action is immediate, and a retry wait would only park the
    caller. Each retryable cause is read from typed provenance the run itself
    recorded, never from a cleanup outcome.
    """
    if run is None:
        return "action_required", CLOSE_REVIEW_RETRY_SECONDS
    if run.terminal_reason == "daemon_stop":
        # The daemon parked this run on its own shutdown, so the cause is known
        # to be a restart rather than anything about the reviewer, and it is
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
