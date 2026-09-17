"""Launch prompt contract for automated oversized task-close reviews."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from gobby.config.feature_base import candidate_runtime_entries, parse_feature_candidate
from gobby.config.tasks import TaskValidationConfig
from gobby.storage.task_close_reviews import (
    TaskCloseReview,
    TaskCloseReviewErrorClass,
    TerminalTaskCloseReviewStatus,
)
from gobby.tasks.validation import NO_WORK_CLOSE_REASONS
from gobby.utils.datetime import utc_now

TASK_CLOSE_VALIDATOR_AGENT = "task-close-validator"
CLOSE_REVIEW_RETRY_SECONDS = 900
# A daemon stop or restart is over in seconds and the daemon is already back up
# by the time the caller reads its payload, so that cause carries its own short
# wait instead of inheriting the 900s provider-pressure default.
CLOSE_REVIEW_DAEMON_STOP_RETRY_SECONDS = 60


def validator_spawn_overrides(
    validation_config: TaskValidationConfig | None,
    *,
    unjudged_attempts: int = 0,
) -> dict[str, str | None]:
    """Return spawn overrides so the validator runs on the ``gobby_tasks.validation`` model.

    The candidate list is ordered, so ``unjudged_attempts`` — how many earlier
    attempts on this task ended without judging the evidence — is how far down
    it this attempt starts, wrapping back to the head once it runs off the end.
    A quota is a passing condition, so the candidate that failed longest ago is
    the one most likely to have recovered; stopping at the tail would strand the
    task on whichever provider stays down longest.
    A candidate's reasoning effort (or the feature profile's default) is forwarded
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
    entry = entries[max(unjudged_attempts, 0) % len(entries)]
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
            "log or receipt. Its criterion_command_gaps entries are compact references to the "
            "unsatisfied criterion_commands entries, which hold the observed evidence; an "
            "omitted_ count states what a bound left out and is never evidence of failure. "
            "Its latest_runs entries retain the verbatim command and state "
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
    elif closure_reason not in NO_WORK_CLOSE_REASONS:
        prompt += (
            "The close caller is not a spawned agent: judge every criterion, including any "
            "beginning `Live:`, as satisfied or gap; `pending_external` is rejected as "
            "malformed for this review. "
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


# The wake contract references blockers; it never re-carries the bulk evidence the
# close payload already reported. The blocking reasons and outstanding_finding_count
# are that reference, and the persisted review row keeps the evidence itself.
_BULK_CLOSE_RESULT_SECTIONS = frozenset(
    {
        "verdict",
        "checklist",
        "transcript_evidence",
        "validation_commands",
        "stable_facts",
    }
)


def build_terminal_review_payload(
    review: TaskCloseReview,
    *,
    status: TerminalTaskCloseReviewStatus,
    close_result: Mapping[str, Any] | None = None,
    message: str | None = None,
    error_class: TaskCloseReviewErrorClass = "action_required",
    retry_seconds: int = CLOSE_REVIEW_RETRY_SECONDS,
) -> dict[str, Any]:
    """Build the persisted automatic-wake contract for one terminal review."""
    result = {
        key: value
        for key, value in (close_result or {}).items()
        if key not in _BULK_CLOSE_RESULT_SECTIONS
    }
    closed = status == "closed"
    retry_after = (
        (utc_now() + timedelta(seconds=retry_seconds)).isoformat()
        if status == "error" and error_class == "retryable_infrastructure"
        else None
    )
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
        required_actions = (
            [f"You may yield while this task stays open. Retry close_task after {retry_after}."]
            if retry_after
            else ["Call close_task again to start a fresh review attempt."]
        )
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
            "error_class": (
                None if closed else "retryable_infrastructure" if retry_after else "action_required"
            ),
            "retry_after": retry_after,
            "validation_status": validation_status,
            "message": message or str(result.get("message") or default_messages[status]),
            "blocking_reasons": blocking_reasons,
            "required_actions": required_actions,
        }
    )
    return result


__all__ = [
    "CLOSE_REVIEW_DAEMON_STOP_RETRY_SECONDS",
    "CLOSE_REVIEW_RETRY_SECONDS",
    "TASK_CLOSE_VALIDATOR_AGENT",
    "build_agentic_review_prompt",
    "build_terminal_review_payload",
]
