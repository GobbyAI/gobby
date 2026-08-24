"""One-shot and agentic task-close criteria review routing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from gobby.config.tasks import TaskValidationConfig
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
    ValidationResult,
    account_criteria_verdict,
    evaluate_criteria_review,
)
from gobby.storage.tasks import Task
from gobby.tasks.close_verdict import CloseVerdictParseError, parse_close_verdict
from gobby.tasks.close_verdict_memo import CloseVerdictMemo
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
    verdict_memo: CloseVerdictMemo | None = None,
) -> ValidationResult:
    """Run one bounded review or account for an authenticated background verdict."""
    if submitted_review is not None:
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

    result = await evaluate_criteria_review(
        task=task,
        task_validator=task_validator,
        ctx=ctx,
        resolved_id=resolved_id,
        changes_summary=changes_summary,
        diff_text=diff_text,
        checklist_facts=checklist_facts,
        validation_config=validation_config,
        reason=reason,
        description=description,
        test_bodies=test_bodies,
        verdict_memo=verdict_memo,
    )
    if result.error_type == "validation_prompt_too_large":
        return ValidationResult(
            can_close=False,
            error_type="agentic_review_required",
            message="Complete close evidence requires a background task-close validator.",
            extra={
                **result.extra,
                "deterministic_evidence_fingerprint": result.extra.get("evidence_fingerprint"),
            },
        )
    return result


__all__ = ["SubmittedCloseReview", "evaluate_close_criteria"]
