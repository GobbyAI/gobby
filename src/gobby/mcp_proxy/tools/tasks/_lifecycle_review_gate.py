"""One-shot and agentic task-close criteria review routing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from gobby.config.tasks import TaskValidationConfig
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
    ValidationResult,
    account_criteria_verdict,
)
from gobby.storage.tasks import Task
from gobby.tasks.close_verdict import CloseVerdictParseError, parse_close_verdict
from gobby.tasks.criteria_contract import missing_operational_evidence, operational_evidence_hint
from gobby.tasks.validation import TaskValidator


@dataclass(frozen=True, slots=True)
class SubmittedCloseReview:
    """Authenticated validator verdict plus the intent fingerprints it must match."""

    verdict: Mapping[str, object]
    review_fingerprint: str
    evidence_fingerprint: str


async def evaluate_close_criteria(
    *,
    task: Task,
    task_validator: TaskValidator,
    ctx: RegistryContext,
    resolved_id: str,
    changes_summary: str,
    diff_text: str,
    checklist_facts: Mapping[str, object],
    validation_config: TaskValidationConfig | None,
    reason: str,
    description: str,
    test_bodies: str,
    submitted_review: SubmittedCloseReview | None = None,
) -> ValidationResult:
    """Detach a new review or account for an authenticated background verdict."""
    raw_transcript_actions = checklist_facts.get("transcript_operational_actions", ())
    transcript_actions = (
        tuple(str(action) for action in raw_transcript_actions)
        if isinstance(raw_transcript_actions, (list, tuple, set, frozenset))
        else ()
    )
    missing_operations = (
        missing_operational_evidence(
            task.validation_criteria,
            changes_summary,
            transcript_actions=transcript_actions,
        )
        if reason == "completed"
        else ()
    )
    if missing_operations:
        missing_text = ", ".join(missing_operations)
        return ValidationResult(
            can_close=False,
            error_type="operational_evidence_missing",
            message=(
                "Task-close evidence does not confirm required operational actions: "
                f"{missing_text}. Record each completed action and outcome in changes_summary "
                "or run a matching successful command in the task transcript."
            ),
            extra={
                "blocking_reasons": [
                    f"Missing completion evidence for operational actions: {missing_text}."
                ],
                "missing_operational_actions": list(missing_operations),
                "operational_evidence_hint": operational_evidence_hint(
                    task.validation_criteria,
                    missing_operations,
                ),
            },
        )
    prepared = task_validator.prepare_task_review(
        title=task.title,
        changes_summary=changes_summary,
        validation_criteria=task.validation_criteria or "",
        diff_text=diff_text,
        checklist_facts=checklist_facts,
        closure_reason=reason,
        description=description,
        test_bodies=test_bodies,
    )
    if submitted_review is not None:
        if (
            prepared.review_fingerprint != submitted_review.review_fingerprint
            or prepared.evidence_fingerprint != submitted_review.evidence_fingerprint
        ):
            return ValidationResult(
                can_close=False,
                error_type="agentic_review_stale",
                message="Task-close evidence changed after the background review launched.",
                extra={
                    "stale_state": True,
                    "review_fingerprint": prepared.review_fingerprint,
                    "deterministic_evidence_fingerprint": prepared.evidence_fingerprint,
                },
            )
        try:
            verdict = parse_close_verdict(
                submitted_review.verdict,
                list(prepared.criteria),
            )
        except CloseVerdictParseError as exc:
            return ValidationResult(
                can_close=False,
                error_type="agentic_review_malformed",
                message=f"Background close-review verdict is invalid: {exc}",
            )
        accounted = account_criteria_verdict(
            task=task,
            verdict=verdict,
            ctx=ctx,
            resolved_id=resolved_id,
            validation_config=validation_config,
            reset_reason="agentic_valid",
        )
        accounted.extra.update(
            {
                "review_fingerprint": prepared.review_fingerprint,
                "deterministic_evidence_fingerprint": prepared.evidence_fingerprint,
            }
        )
        return accounted

    return ValidationResult(
        can_close=False,
        error_type="agentic_review_required",
        message=(
            "Complete close evidence requires a daemon-managed task-close validator; "
            "its verdict is applied and delivered automatically."
        ),
        extra={
            "prompt_chars": prepared.prompt_chars,
            "prompt_limit": prepared.prompt_limit,
            "review_fingerprint": prepared.review_fingerprint,
            "deterministic_evidence_fingerprint": prepared.evidence_fingerprint,
            "manifest_count": prepared.manifest_count,
            "excerpt_chars": prepared.excerpt_chars,
        },
    )


__all__ = ["SubmittedCloseReview", "evaluate_close_criteria"]
