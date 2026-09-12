"""Launch prompt contract for automated oversized task-close reviews."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from gobby.config.feature_base import candidate_runtime_entries, parse_feature_candidate
from gobby.config.tasks import TaskValidationConfig
from gobby.storage.task_close_reviews import TaskCloseReview, TerminalTaskCloseReviewStatus
from gobby.tasks.validation import NO_WORK_CLOSE_REASONS

TASK_CLOSE_VALIDATOR_AGENT = "task-close-validator"


def validator_spawn_overrides(
    validation_config: TaskValidationConfig | None,
    *,
    provider_failures: int = 0,
) -> dict[str, str | None]:
    """Return spawn overrides so the validator runs on the ``gobby_tasks.validation`` model.

    The candidate list is ordered, so ``provider_failures`` — how many earlier
    attempts on this task died on their provider rather than on the evidence —
    is how far down it this attempt starts, clamped to the last candidate. A
    candidate's reasoning effort (or the feature profile's default) is forwarded
    only when it resolves to a concrete value; an unpinned candidate leaves the
    agent definition's own effort default in force, since an explicit ``None``
    would suppress it. Without a validation config the definition's defaults
    apply entirely.
    """
    if validation_config is None:
        return {}
    entries = candidate_runtime_entries(
        validation_config.candidates, profile=validation_config.profile
    )
    if not entries:
        return {}
    entry = entries[min(max(provider_failures, 0), len(entries) - 1)]
    provider, model = parse_feature_candidate(entry.candidate)
    overrides: dict[str, str | None] = {"provider": provider, "model": model}
    if entry.reasoning_effort is not None:
        overrides["reasoning_effort"] = entry.reasoning_effort
    return overrides


def build_agentic_review_prompt(
    *,
    review_id: str,
    task_id: str,
    commit_shas: Sequence[str],
    changes_summary: str,
    review_fingerprint: str,
    evidence_fingerprint: str,
    closure_reason: str = "completed",
    validation_commands: Mapping[str, object] | None = None,
    prior_requirements: str | None = None,
    coordinator_owned_pending: bool = False,
) -> str:
    """Build the fixed taskless validator prompt for one persisted review intent.

    ``validation_commands`` is gate 10's transcript-derived run record. The
    validator has no other access to it; without it the validator re-derives
    its own evidence standard and asks for receipts gate 10 already holds.
    """
    prompt = (
        "Perform the read-only task-close review. "
        f"review_id={review_id}; task_id={task_id}; "
        f"commit_shas={json.dumps(list(commit_shas))}; "
        f"changes_summary={json.dumps(changes_summary)}; "
        f"closure_reason={json.dumps(closure_reason)}; "
        f"review_fingerprint={review_fingerprint}; "
        f"deterministic_evidence_fingerprint={evidence_fingerprint}. "
    )
    if closure_reason in NO_WORK_CLOSE_REASONS:
        prompt += (
            "This is a no-work disposition review. The original implementation criteria are "
            "not expected to be met. Judge changes_summary as the disposition justification: "
            "mark each criterion satisfied when it coherently and specifically explains the "
            "duplicate target, existing implementation, deliberate wont-fix decision, "
            "obsolescence, or external ownership. Reject only a missing, vague, or contradicted "
            "justification. Do not require implementation, tests, commits, or operational "
            "actions for superseded deliverables. Deterministic gates still own attributed "
            "edits, commits, dirty paths, and ownership. "
        )
    if validation_commands is not None:
        facts = json.dumps(validation_commands, sort_keys=True, default=str)
        prompt += (
            f"validation_commands={facts}. "
            "validation_commands is gate 10's authoritative transcript record of commands task "
            "sessions ran. Its criterion_commands entries apply the same normalization contract "
            "to criterion commands and transcript core commands, including approved environment "
            "and directory prefixes; a satisfied entry is authoritative without any committed "
            "log or receipt. Its latest_runs entries retain the verbatim command and state "
            "core_command and wrapped. Its uncredited_runs entries name commands seen but "
            "excluded because their outcome was unknown, they were wrapped, or they were stale "
            "after a later edit; cite that entry when a verdict names a seen-but-uncredited run. "
            "Review every command requirement and report every command gap in one verdict. "
        )
    if coordinator_owned_pending and closure_reason not in NO_WORK_CLOSE_REASONS:
        prompt += (
            "The close caller is a spawned agent. For every criterion beginning `Live:` "
            "case-insensitively, report state `pending_external`, satisfied false, gap null, "
            "and required_evidence null. Count it as neither satisfied nor a gap. Set the "
            "overall status valid when every remaining implementer-owned criterion is satisfied. "
        )
    prompt += (
        "Inspect the task and evidence relevant to the stated closure reason. "
        "For completed work, inspect linked commits, exact acceptance tests, deterministic "
        "gate facts, and repository validations. Call submit_close_review with this exact "
        "review_id and "
        "only the structured verdict object, correct any rejected malformed submission, then "
        "call end_agent_run. Do not mutate tasks, spawn agents, or stop other agent runs."
    )
    if not prior_requirements:
        return prompt
    if closure_reason in NO_WORK_CLOSE_REASONS:
        return (
            f"{prompt} prior_requirements={json.dumps(prior_requirements)}. "
            "Apply prior requirements to the disposition justification only. Requirements "
            "demanding superseded implementation are inapplicable to this closure reason."
        )
    return (
        f"{prompt} prior_requirements={json.dumps(prior_requirements)}. "
        "A previously rejected close named this required evidence; reject the same criterion "
        "again unless the submitted code and evidence satisfy it."
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
    validation_status = (
        "valid"
        if closed
        else "invalid"
        if status == "invalid"
        else "pending"
        if status == "external_pending"
        else "error"
    )
    default_messages = {
        "closed": "Task closed after background validation.",
        "invalid": "Background validation found blocking task-close gaps.",
        "external_pending": "Coordinator-owned live criteria remain pending verification.",
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
    elif not required_actions and status == "external_pending":
        required_actions = [
            "End this agent run; the coordinator must verify the pending Live criteria and close the task."
        ]
    elif not required_actions and status == "stale":
        required_actions = ["Call close_task again with the current task and commit evidence."]
    elif not required_actions and status == "error":
        required_actions = ["Call close_task again to start a fresh review attempt."]
    if status == "invalid":
        result["outstanding_finding_count"] = len(blocking_reasons)
        result["remediation_guidance"] = (
            "Address every listed blocking reason, validate the complete fix set, and commit the "
            "complete fix set before one resubmission."
        )
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
