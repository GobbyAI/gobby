"""One-shot and agentic task-close criteria review routing."""

from __future__ import annotations

from collections.abc import Mapping

from gobby.config.tasks import TaskValidationConfig
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
    ValidationResult,
    account_criteria_verdict,
    evaluate_criteria_review,
)
from gobby.storage.tasks import Task
from gobby.tasks.agentic_close_review import (
    build_agentic_review_request,
    validate_agentic_review_run,
)
from gobby.tasks.close_verdict import CloseVerdictParseError, parse_close_verdict
from gobby.tasks.criteria_contract import split_validation_criteria
from gobby.tasks.validation import TaskValidator


async def evaluate_close_criteria(
    *,
    task: Task,
    task_validator: TaskValidator,
    ctx: RegistryContext,
    resolved_id: str,
    parent_session_id: str,
    changes_summary: str,
    commit_shas: list[str],
    diff_text: str,
    checklist_facts: Mapping[str, object],
    validation_config: TaskValidationConfig | None,
    reason: str,
    description: str,
    test_bodies: str,
    review_run_id: str | None,
) -> ValidationResult:
    """Run the one-shot review or validate a matching oversized-review agent run."""
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
    )
    if result.error_type != "validation_prompt_too_large":
        return result

    close_fingerprint = str(result.extra.get("review_fingerprint") or "")
    evidence_fingerprint = str(result.extra.get("evidence_fingerprint") or "")
    if not close_fingerprint or not evidence_fingerprint:
        return result
    request = build_agentic_review_request(
        task_id=resolved_id,
        commit_shas=commit_shas,
        changes_summary=changes_summary,
        close_fingerprint=close_fingerprint,
        evidence_fingerprint=evidence_fingerprint,
    )
    if not review_run_id:
        return ValidationResult(
            can_close=False,
            error_type="agentic_review_required",
            message=(
                "Complete close evidence exceeds the one-shot review limit. "
                "Run the fixed task-close-validator request and retry with review_run_id."
            ),
            extra={**result.extra, **request},
        )

    check = validate_agentic_review_run(
        db=ctx.task_manager.db,
        review_run_id=review_run_id,
        parent_session_id=parent_session_id,
        task_id=resolved_id,
        commit_shas=commit_shas,
        changes_summary=changes_summary,
        close_fingerprint=close_fingerprint,
        evidence_fingerprint=evidence_fingerprint,
    )
    if check.state != "ready" or check.verdict is None:
        return ValidationResult(
            can_close=False,
            error_type=check.error_type or "agentic_review_failed",
            message=check.message,
            extra={**result.extra, **request, "review_run_id": review_run_id},
        )
    criteria = split_validation_criteria(task.validation_criteria or "")
    try:
        verdict = parse_close_verdict(check.verdict, criteria)
    except CloseVerdictParseError as exc:
        return ValidationResult(
            can_close=False,
            error_type="agentic_review_malformed",
            message=f"Agent review structured verdict is invalid: {exc}",
            extra={**result.extra, "review_run_id": review_run_id},
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
            "review_run_id": review_run_id,
            "review_fingerprint": close_fingerprint,
            "deterministic_evidence_fingerprint": evidence_fingerprint,
        }
    )
    return accounted


__all__ = ["evaluate_close_criteria"]
