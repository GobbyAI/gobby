"""Small validation helpers for the task-close checklist."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gobby.ai.text_generation import is_feature_generation_infrastructure_error
from gobby.config.tasks import TaskValidationConfig
from gobby.failure_categories import FailureCategory, classify_exception
from gobby.mcp_proxy.tools._task_query_pagination import collect_task_query_pages
from gobby.mcp_proxy.tools.tasks._escalation_coordinator import coordinate_task_escalation
from gobby.storage.tasks import Task, TaskAlreadyEscalatedError, TaskStaleStateError
from gobby.storage.tasks._validation_backoff import TaskValidationBackoffStore
from gobby.tasks.close_verdict import CloseVerdict, CloseVerdictParseError
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_closed
from gobby.tasks.validation import ValidationPromptTooLarge
from gobby.tasks.validation_history import ValidationHistoryManager
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.tasks.validation import TaskValidator


@dataclass
class ValidationResult:
    can_close: bool
    error_type: str | None = None
    message: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    failure_category: FailureCategory | None = None
    validation_status: str | None = None
    validation_feedback: str | None = None
    reset_reason: str | None = None


def validate_commit_requirements(
    task: Task,
    reason: str,
    repo_path: str | None = None,
) -> ValidationResult:
    """Require and resolve linked commits when a leaf has attributed edits."""
    del reason
    if not task.commits:
        return ValidationResult(
            can_close=False,
            error_type="no_commits_linked",
            message=(
                "This task has attributed edits but no linked commit. Commit the task edits and "
                "pass the resulting SHA to close_task."
            ),
        )
    if repo_path:
        from gobby.utils.git import normalize_commit_sha

        stale = [sha for sha in task.commits if normalize_commit_sha(sha, cwd=repo_path) is None]
        if stale:
            return ValidationResult(
                can_close=False,
                error_type="stale_commits",
                message=(
                    "Linked commits no longer exist in the task repository: "
                    f"{', '.join(stale)}. Link the current commit set and retry."
                ),
                extra={"stale_shas": stale},
            )
    return ValidationResult(can_close=True)


def validate_parent_task(
    ctx: RegistryContext,
    task_id: str,
    *,
    children: Sequence[Task] | None = None,
) -> ValidationResult:
    """Require every child of a structural parent to be closed."""
    if children is None:
        children = collect_task_query_pages(
            ctx.task_manager.list_tasks,
            parent_task_id=task_id,
        )
    open_children = [child for child in children if not is_task_closed(child)]
    if not open_children:
        return ValidationResult(can_close=True)
    refs = [
        f"#{seq_num}" if (seq_num := getattr(child, "seq_num", None)) else child.id
        for child in open_children[:5]
    ]
    suffix = f" and {len(open_children) - 5} more" if len(open_children) > 5 else ""
    return ValidationResult(
        can_close=False,
        error_type="children_open",
        message=(
            f"Close every child task before closing this parent. Open children: "
            f"{', '.join(refs)}{suffix}."
        ),
        extra={"open_children": [child.id for child in open_children]},
    )


def active_validation_backoff(task: Task, ctx: RegistryContext) -> ValidationResult | None:
    """Return an actionable result when the task is still in infra backoff."""
    state = TaskValidationBackoffStore(ctx.task_manager.db).get(task.id)
    now = utc_now()
    if state is None or not state.is_in_backoff_window(now):
        return None
    retry_after = max(
        1,
        math.ceil((state.next_retry_at - now).total_seconds()) if state.next_retry_at else 1,
    )
    return ValidationResult(
        can_close=False,
        error_type="validation_infrastructure_unavailable",
        message=f"Validation infrastructure is in backoff; retry after {retry_after} seconds.",
        extra={
            "retryable": True,
            "retry_after": retry_after,
            "next_retry_at": state.next_retry_at.isoformat() if state.next_retry_at else None,
            "consecutive_failures": state.consecutive_failures,
        },
        failure_category=FailureCategory.PROVIDER,
    )


def record_validation_infrastructure_failure(
    task: Task,
    ctx: RegistryContext,
    *,
    resolved_id: str,
    message: str,
    error_type: str = "validation_infrastructure_unavailable",
    failure_category: FailureCategory = FailureCategory.PROVIDER,
) -> ValidationResult:
    """Persist one infrastructure failure, one history row, and optional escalation."""
    store = TaskValidationBackoffStore(ctx.task_manager.db)
    state = store.record_failure(task.id, error=message, now=utc_now())
    ctx.task_manager.update_task(
        resolved_id,
        validation_status="error",
        validation_feedback=message,
    )
    _record_validation_iteration(
        task,
        ctx,
        status="error",
        feedback=message,
        failure_category=failure_category,
    )
    retry_after = 1
    if state.next_retry_at:
        retry_after = max(1, math.ceil((state.next_retry_at - utc_now()).total_seconds()))
    extra: dict[str, Any] = {
        "retryable": not state.should_escalate(),
        "retry_after": retry_after,
        "next_retry_at": state.next_retry_at.isoformat() if state.next_retry_at else None,
        "consecutive_failures": state.consecutive_failures,
    }
    if state.should_escalate():
        escalated = ctx.task_manager.escalate_task(
            resolved_id,
            reason=(
                "validation generation unavailable after "
                f"{state.consecutive_failures} consecutive infrastructure failures"
            ),
        )
        event_id = coordinate_task_escalation(
            ctx,
            escalated,
            prior_owner_session_id=get_claimed_session_id(task),
            session_id=None,
        )
        extra.update({"escalated": True, "escalation_event_id": event_id})
    return ValidationResult(
        can_close=False,
        error_type=error_type,
        message=message,
        extra=extra,
        failure_category=failure_category,
    )


def account_criteria_verdict(
    *,
    task: Task,
    verdict: CloseVerdict,
    ctx: RegistryContext,
    resolved_id: str,
    validation_config: TaskValidationConfig | None,
    reset_reason: str,
) -> ValidationResult:
    """Apply shared validation history, feedback, failure, and escalation behavior."""
    store = TaskValidationBackoffStore(ctx.task_manager.db)
    if store.get(task.id) is not None:
        store.clear(task.id)
    _record_validation_iteration(
        task,
        ctx,
        status=verdict.status,
        feedback=verdict.feedback,
        failure_category=None if verdict.valid else FailureCategory.CODE,
    )
    verdict_dict = verdict.to_dict()
    if verdict.valid:
        return ValidationResult(
            can_close=True,
            extra={"verdict": verdict_dict},
            validation_status="valid",
            validation_feedback=verdict.feedback,
            reset_reason=reset_reason,
        )

    threshold = validation_config.close_validation_escalation_threshold if validation_config else 5
    try:
        fail_count, escalated_now = ctx.task_manager.increment_validation_failure(
            resolved_id,
            expected_updated_at=task.updated_at,
            threshold=threshold,
            validation_status="invalid",
            validation_feedback=verdict.feedback,
            escalation_reason=(
                f"close validation remained invalid after reaching the {threshold}-attempt threshold"
            ),
        )
    except TaskStaleStateError as exc:
        return ValidationResult(
            can_close=False,
            error_type="stale_task_state",
            message=str(exc),
            extra={"stale_state": True, "verdict": verdict_dict},
        )
    except TaskAlreadyEscalatedError as exc:
        return ValidationResult(
            can_close=False,
            error_type="validation_failed",
            message=str(exc),
            extra={"escalated": True, "already_escalated": True},
        )
    extra = {"validation_fail_count": fail_count, "verdict": verdict_dict}
    if escalated_now:
        escalated = ctx.task_manager.get_task(resolved_id)
        event_id = coordinate_task_escalation(
            ctx,
            escalated,
            prior_owner_session_id=get_claimed_session_id(task),
            session_id=None,
        )
        extra.update({"escalated": True, "escalation_event_id": event_id})
    gaps = [
        criterion.gap for criterion in verdict.criteria if not criterion.satisfied and criterion.gap
    ]
    return ValidationResult(
        can_close=False,
        error_type="validation_failed",
        message=verdict.feedback,
        extra={**extra, "blocking_reasons": gaps or [verdict.feedback]},
        failure_category=FailureCategory.CODE,
        validation_status="invalid",
        validation_feedback=verdict.feedback,
    )


async def evaluate_criteria_review(
    *,
    task: Task,
    task_validator: TaskValidator,
    ctx: RegistryContext,
    resolved_id: str,
    changes_summary: str,
    diff_text: str | None,
    checklist_facts: Mapping[str, object],
    validation_config: TaskValidationConfig | None,
    reason: str = "completed",
    description: str = "",
    test_bodies: str = "Named acceptance tests: none.",
) -> ValidationResult:
    """Run and account for exactly one bounded LLM criteria review."""
    backoff = active_validation_backoff(task, ctx)
    if backoff is not None:
        return backoff
    try:
        verdict = await task_validator.validate_task(
            task_id=task.id,
            title=task.title,
            changes_summary=changes_summary,
            validation_criteria=task.validation_criteria or "",
            diff_text=diff_text,
            checklist_facts=checklist_facts,
            closure_reason=reason,
            description=description,
            test_bodies=test_bodies,
        )
    except ValidationPromptTooLarge as exc:
        return ValidationResult(
            can_close=False,
            error_type="validation_prompt_too_large",
            message=str(exc),
            extra={
                "prompt_chars": exc.prompt_chars,
                "prompt_limit": exc.prompt_limit,
                "review_fingerprint": exc.review_fingerprint,
                "evidence_fingerprint": exc.evidence_fingerprint,
            },
        )
    except CloseVerdictParseError as exc:
        return record_validation_infrastructure_failure(
            task,
            ctx,
            resolved_id=resolved_id,
            message=f"Validation provider returned an unusable response: {exc}",
            failure_category=FailureCategory.PROVIDER,
        )
    except Exception as exc:
        if not is_feature_generation_infrastructure_error(exc):
            raise
        return record_validation_infrastructure_failure(
            task,
            ctx,
            resolved_id=resolved_id,
            message=f"Validation generation unavailable: {exc}",
            failure_category=classify_exception(exc),
        )

    return account_criteria_verdict(
        task=task,
        verdict=verdict,
        ctx=ctx,
        resolved_id=resolved_id,
        validation_config=validation_config,
        reset_reason="llm_valid",
    )


def determine_close_outcome(
    task: Task,
    skip_validation: bool,
    override_justification: str | None,
) -> tuple[bool, bool]:
    """Return organizational override audit flags."""
    justified_escalated_close = task.is_escalated and bool((override_justification or "").strip())
    return False, skip_validation or justified_escalated_close


def _record_validation_iteration(
    task: Task,
    ctx: RegistryContext,
    *,
    status: str,
    feedback: str | None,
    failure_category: FailureCategory | None,
) -> int:
    history = ValidationHistoryManager(ctx.task_manager.db)
    latest = history.get_latest_iteration(task.id)
    iteration = latest.iteration + 1 if latest else 1
    history.record_iteration(
        task_id=task.id,
        iteration=iteration,
        status=status,
        feedback=feedback,
        issues=[],
        context_type="close_checklist",
        context_summary="Bounded task-close criteria review",
        validator_type="llm",
        failure_category=failure_category,
    )
    return iteration


__all__ = [
    "ValidationResult",
    "account_criteria_verdict",
    "active_validation_backoff",
    "determine_close_outcome",
    "evaluate_criteria_review",
    "record_validation_infrastructure_failure",
    "validate_commit_requirements",
    "validate_parent_task",
]
