"""Agent-run handoff contract for oversized task-close reviews."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase

TASK_CLOSE_VALIDATOR_AGENT = "task-close-validator"


@dataclass(frozen=True, slots=True)
class AgenticReviewCheck:
    """Validation result for one supplied agent run."""

    state: Literal["ready", "pending", "error"]
    error_type: str | None
    message: str
    verdict: dict[str, Any] | None = None


def build_agentic_review_request(
    *,
    task_id: str,
    commit_shas: list[str],
    changes_summary: str,
    close_fingerprint: str,
    evidence_fingerprint: str,
) -> dict[str, object]:
    """Build fixed taskless spawn and retry instructions."""
    prompt = (
        "Perform the read-only oversized close review. "
        f"task_id={task_id}; commit_shas={json.dumps(commit_shas)}; "
        f"changes_summary={json.dumps(changes_summary)}; "
        f"close_fingerprint={close_fingerprint}; "
        f"deterministic_evidence_fingerprint={evidence_fingerprint}. "
        "Inspect the task, linked commits, exact acceptance tests, deterministic "
        "gate facts, and repository validations. Return the task-close-validator "
        "JSON contract exactly; do not mutate or close any task."
    )
    return {
        "agentic_review_required": True,
        "review_fingerprint": close_fingerprint,
        "deterministic_evidence_fingerprint": evidence_fingerprint,
        "spawn_request": {
            "agent": TASK_CLOSE_VALIDATOR_AGENT,
            "task_id": None,
            "prompt": prompt,
        },
        "retry": (
            "Wait for the run to finish, then retry close_task with unchanged close "
            "arguments and review_run_id set to that run id."
        ),
    }


def validate_agentic_review_run(
    *,
    db: HubDatabase,
    review_run_id: str,
    parent_session_id: str,
    task_id: str,
    commit_shas: list[str],
    changes_summary: str,
    close_fingerprint: str,
    evidence_fingerprint: str,
) -> AgenticReviewCheck:
    """Fail closed unless a completed taskless validator run matches current evidence."""
    run = LocalAgentRunManager(db).get(review_run_id)
    if run is None:
        return _error(
            "agentic_review_not_found", f"Agent review run {review_run_id} was not found."
        )
    if run.agent_name != TASK_CLOSE_VALIDATOR_AGENT:
        return _error(
            "agentic_review_wrong_agent",
            f"Run {review_run_id} was not produced by {TASK_CLOSE_VALIDATOR_AGENT}.",
        )
    if run.task_id is not None:
        return _error(
            "agentic_review_wrong_agent",
            "Task-close validator runs must be taskless.",
        )
    if run.parent_session_id != parent_session_id:
        return _error(
            "agentic_review_wrong_parent",
            "Agent review run belongs to a different closing session.",
        )
    if run.status in {"pending", "running"}:
        return AgenticReviewCheck(
            state="pending",
            error_type="agentic_review_pending",
            message=f"Agent review run {review_run_id} is still {run.status}.",
        )
    if run.status != "success":
        return _error(
            "agentic_review_failed",
            f"Agent review run {review_run_id} ended with status {run.status}.",
        )
    if close_fingerprint not in run.prompt:
        return _error(
            "agentic_review_stale",
            "Agent review launch prompt does not match the current close fingerprint.",
        )
    try:
        payload = json.loads(run.result or "")
    except json.JSONDecodeError:
        return _error("agentic_review_malformed", "Agent review result is not a JSON object.")
    if not isinstance(payload, dict):
        return _error("agentic_review_malformed", "Agent review result is not a JSON object.")

    expected: dict[str, object] = {
        "schema_version": 1,
        "agent": TASK_CLOSE_VALIDATOR_AGENT,
        "task_id": task_id,
        "commit_shas": commit_shas,
        "changes_summary": changes_summary,
        "close_fingerprint": close_fingerprint,
        "deterministic_evidence_fingerprint": evidence_fingerprint,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            return _error(
                "agentic_review_stale",
                f"Agent review result field {key!r} does not match the current close request.",
            )
    verdict = payload.get("verdict")
    if not isinstance(verdict, dict):
        return _error(
            "agentic_review_malformed",
            "Agent review result omitted its structured verdict.",
        )
    return AgenticReviewCheck(
        state="ready",
        error_type=None,
        message="Completed agent review matches the current close evidence.",
        verdict=verdict,
    )


def _error(error_type: str, message: str) -> AgenticReviewCheck:
    return AgenticReviewCheck(state="error", error_type=error_type, message=message)


__all__ = [
    "AgenticReviewCheck",
    "TASK_CLOSE_VALIDATOR_AGENT",
    "build_agentic_review_request",
    "validate_agentic_review_run",
]
