"""Launch prompt contract for automated oversized task-close reviews."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from gobby.storage.task_close_reviews import TaskCloseReview, TerminalTaskCloseReviewStatus

TASK_CLOSE_VALIDATOR_AGENT = "task-close-validator"


def build_agentic_review_prompt(
    *,
    review_id: str,
    task_id: str,
    commit_shas: Sequence[str],
    changes_summary: str,
    review_fingerprint: str,
    evidence_fingerprint: str,
) -> str:
    """Build the fixed taskless validator prompt for one persisted review intent."""
    return (
        "Perform the read-only oversized task-close review. "
        f"review_id={review_id}; task_id={task_id}; "
        f"commit_shas={json.dumps(list(commit_shas))}; "
        f"changes_summary={json.dumps(changes_summary)}; "
        f"review_fingerprint={review_fingerprint}; "
        f"deterministic_evidence_fingerprint={evidence_fingerprint}. "
        "Inspect the task, linked commits, exact acceptance tests, deterministic gate facts, "
        "and repository validations. Call submit_close_review with this exact review_id and "
        "only the structured verdict object, correct any rejected malformed submission, then "
        "call end_agent_run. Do not mutate tasks, spawn agents, or stop other agent runs."
    )


def build_terminal_review_payload(
    review: TaskCloseReview,
    *,
    status: TerminalTaskCloseReviewStatus,
    close_result: Mapping[str, Any] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Build the persisted automatic-wake contract for one terminal review."""
    result = dict(close_result or {})
    closed = status == "closed"
    validation_status = "valid" if closed else "invalid" if status == "invalid" else "error"
    default_messages = {
        "closed": "Task closed after background validation.",
        "invalid": "Background validation found blocking task-close gaps.",
        "stale": "Task-close evidence changed while the background review was running.",
        "error": "Background task-close validation could not finish.",
    }
    blocking_reasons = list(result.get("blocking_reasons") or [])
    if status == "invalid" and not blocking_reasons and message:
        blocking_reasons = [message]
    required_actions = list(result.get("required_actions") or [])
    if not required_actions and status == "invalid":
        required_actions = [
            "Address every blocking reason, rerun focused validation, commit fixes, and call close_task again."
        ]
    elif not required_actions and status == "stale":
        required_actions = ["Call close_task again with the current task and commit evidence."]
    elif not required_actions and status == "error":
        required_actions = ["Call close_task again to start a fresh review attempt."]
    result.update(
        {
            "event": "task_close_review_completed",
            "review_id": review.id,
            "run_id": review.agent_run_id,
            "task_id": review.task_id,
            "task_ref": review.task_ref,
            "status": status,
            "closed": closed,
            "validation_status": validation_status,
            "message": message or str(result.get("message") or default_messages[status]),
            "blocking_reasons": blocking_reasons,
            "required_actions": required_actions,
        }
    )
    return result


__all__ = [
    "TASK_CLOSE_VALIDATOR_AGENT",
    "build_agentic_review_prompt",
    "build_terminal_review_payload",
]
