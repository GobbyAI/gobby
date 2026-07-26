"""Validation helpers for task lifecycle operations.

Provides validation functions used by close_task to verify tasks
can be closed (commit checks, child completion, LLM validation).
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.config.tasks import TaskValidationConfig
from gobby.failure_categories import (
    FailureCategory,
    classify_failure,
    persisted_validation_status,
)
from gobby.mcp_proxy.tools._task_query_pagination import collect_task_query_pages
from gobby.mcp_proxy.tools.tasks._escalation_coordinator import coordinate_task_escalation
from gobby.mcp_proxy.tools.tasks._helpers import SKIP_REASONS
from gobby.storage.tasks import Task, TaskStaleStateError
from gobby.storage.tasks._validation_backoff import TaskValidationBackoffStore
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_closed
from gobby.tasks.validation_history import ValidationHistoryManager
from gobby.tasks.validation_models import Issue
from gobby.tasks.validation_verdict import format_close_validation_message
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.storage.tasks import LocalTaskManager
    from gobby.tasks.validation import TaskValidator

logger = logging.getLogger(__name__)

__all__ = [
    "ValidationContext",
    "ValidationResult",
    "gather_validation_context",
    "validate_commit_requirements",
    "validate_leaf_task_with_llm",
    "validate_parent_task",
]


@dataclass
class ValidationResult:
    """Result of validation checks."""

    can_close: bool
    error_type: str | None = None
    message: str | None = None
    extra: dict[str, Any] | None = None
    validation_status: str | None = None
    validation_feedback: str | None = None
    reset_reason: str | None = None
    failure_category: FailureCategory | None = None


@dataclass(frozen=True)
class ValidationContext:
    """Evidence packet passed from close_task into LLM validation."""

    validation_context: str | None
    raw_diff: str | None
    file_context_text: str | None = None
    is_documentation_only: bool = False


def _record_validation_iteration(
    task: Task,
    ctx: "RegistryContext",
    *,
    status: str,
    feedback: str | None,
    context_type: str,
    validator_type: str = "llm",
    failure_category: FailureCategory | None = None,
    issues: list[Issue] | None = None,
) -> int:
    """Record one live validation attempt with a monotonic task-local number."""
    history_manager = ValidationHistoryManager(ctx.task_manager.db)
    latest = history_manager.get_latest_iteration(task.id)
    iteration = latest.iteration + 1 if latest else 1
    history_manager.record_iteration(
        task_id=task.id,
        iteration=iteration,
        status=status,
        feedback=feedback,
        issues=issues,
        context_type=context_type,
        context_summary="Live close-task validation",
        validator_type=validator_type,
        failure_category=failure_category,
    )
    return iteration


def _path_matches_reference(path: str, reference: str) -> bool:
    normalized_path = path.replace("\\", "/").lstrip("./")
    normalized_reference = reference.replace("\\", "/").lstrip("./")
    if not normalized_path or not normalized_reference:
        return False
    if normalized_path == normalized_reference:
        return True
    if normalized_path.endswith(f"/{normalized_reference}"):
        return True
    return "/" not in normalized_reference and Path(normalized_path).name == normalized_reference


def _resolve_within_base(candidate: Path, base_path: Path) -> Path | None:
    try:
        resolved_candidate = candidate.resolve()
        resolved_base = base_path.resolve()
        resolved_candidate.relative_to(resolved_base)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved_candidate


def _resolve_referenced_files(
    *,
    mentioned_files: list[str],
    changed_files: list[str],
    repo_path: str | None,
    max_files: int = 5,
) -> list[Path]:
    """Resolve task-mentioned files that are not part of the linked diff."""
    if not mentioned_files:
        return []

    from gobby.utils.git import run_git_command

    base_path = (Path(repo_path) if repo_path else Path.cwd()).resolve()
    tracked_output = run_git_command(["git", "ls-files"], cwd=base_path) or ""
    tracked_files = [line.strip() for line in tracked_output.splitlines() if line.strip()]
    resolved: list[Path] = []

    for mention in mentioned_files:
        reference = mention.strip().strip("`'\"").lstrip("./")
        if not reference:
            continue
        if any(_path_matches_reference(changed_file, reference) for changed_file in changed_files):
            continue

        candidates: list[Path] = []
        direct = Path(reference)
        candidates.append(direct if direct.is_absolute() else base_path / direct)
        candidates.extend(
            base_path / tracked_file
            for tracked_file in tracked_files
            if _path_matches_reference(tracked_file, reference)
        )

        for candidate in candidates:
            if len(resolved) >= max_files:
                return resolved
            safe_candidate = _resolve_within_base(candidate, base_path)
            if safe_candidate and safe_candidate.is_file() and safe_candidate not in resolved:
                resolved.append(safe_candidate)

    return resolved


def _read_referenced_file_context(
    *,
    mentioned_files: list[str],
    changed_files: list[str],
    repo_path: str | None,
    max_chars: int,
) -> str | None:
    from gobby.tasks.validation import read_files_content

    files = _resolve_referenced_files(
        mentioned_files=mentioned_files,
        changed_files=changed_files,
        repo_path=repo_path,
    )
    if not files:
        return None

    content = read_files_content(files, max_chars=max_chars)
    return (
        "Referenced current file context "
        "(task-mentioned files not present in the linked commit diff):\n"
        f"{content}"
    )


def validate_commit_requirements(
    task: Task,
    reason: str,
    repo_path: str | None = None,
) -> ValidationResult:
    """Check if task meets commit requirements for closing.

    Args:
        task: The task to validate
        reason: Reason for closing
        repo_path: Path to the repository for git operations

    Returns:
        ValidationResult indicating if task can be closed
    """
    # Skip commit check for certain close reasons that imply no work was done
    requires_commit_check = reason.lower() not in SKIP_REASONS

    if requires_commit_check and not task.commits:
        return ValidationResult(
            can_close=False,
            error_type="no_commits_linked",
            message=(
                "\nA commit is required before closing this task.\n\n"
                "**Normal flow:**\n"
                "1. Commit your changes: "
                'git commit -m "[<project_name>-#<task_number>] <type>: <description>"\n'
                '2. Close with commit_sha: close_task(task_id="#N", commit_sha="<sha>")\n\n'
                "**Edge cases (no work done):**\n"
                '- Task was already done: reason="already_implemented"\n'
                '- Task is no longer needed: reason="obsolete"\n'
                '- Task duplicates another: reason="duplicate"\n'
                '- Decided not to do it: reason="wont_fix"\n'
                '- Changes outside repo (e.g., ~/.gobby/bootstrap.yaml): reason="out_of_repo"'
            ),
        )

    # Re-verify stored SHAs actually exist as commits in the repo
    if requires_commit_check and task.commits and repo_path:
        from gobby.utils.git import normalize_commit_sha

        stale_shas = [sha for sha in task.commits if not normalize_commit_sha(sha, cwd=repo_path)]
        if stale_shas:
            return ValidationResult(
                can_close=False,
                error_type="stale_commits",
                message=(
                    f"Linked commit(s) no longer exist in the repository: "
                    f"{', '.join(stale_shas)}\n\n"
                    "These SHAs may have been rebased away or linked from a different repo.\n"
                    "Unlink them and link the correct commit SHA before closing."
                ),
                extra={"stale_shas": stale_shas},
            )

    return ValidationResult(can_close=True)


def validate_parent_task(
    ctx: "RegistryContext",
    task_id: str,
) -> ValidationResult:
    """Check if a parent task's children are all closed.

    Args:
        ctx: Registry context
        task_id: The parent task ID

    Returns:
        ValidationResult indicating if parent can be closed
    """
    children = collect_task_query_pages(
        ctx.task_manager.list_tasks,
        parent_task_id=task_id,
    )

    if children:
        open_children = [c for c in children if not is_task_closed(c)]
        if open_children:
            open_titles = [f"- {c.id}: {c.title}" for c in open_children[:5]]
            remaining = len(open_children) - 5 if len(open_children) > 5 else 0
            feedback = f"Cannot close: {len(open_children)} child tasks still open:\n"
            feedback += "\n".join(open_titles)
            if remaining > 0:
                feedback += f"\n... and {remaining} more"
            return ValidationResult(
                can_close=False,
                error_type="validation_failed",
                message=feedback,
                extra={"open_children": [c.id for c in open_children]},
            )

    return ValidationResult(can_close=True)


def gather_validation_context(
    task: Task,
    changes_summary: str | None,
    repo_path: str | None,
    task_manager: "LocalTaskManager",
) -> ValidationContext:
    """Gather context for LLM validation.

    Uses provided changes_summary or auto-fetches via smart context gathering.

    Args:
        task: The task to validate
        changes_summary: Optional user-provided summary
        repo_path: Path to the repository
        task_manager: LocalTaskManager for fetching task diff

    Returns:
        Structured validation evidence containing summarized changes, raw diff,
        and optional referenced-file context.
    """
    from gobby.tasks.commits import (
        DOC_EXTENSIONS,
        changed_files_from_diff,
        collect_task_diff_text,
        extract_mentioned_files,
    )
    from gobby.tasks.validation import (
        VALIDATION_FILE_CONTEXT_BUDGET_CHARS,
        VALIDATION_PROMPT_BUDGET_CHARS,
    )
    from gobby.tasks.validation_evidence import build_diff_validation_evidence

    validation_context = ""
    raw_diff = None
    file_context_text = None
    changes_summary_included = False
    is_documentation_only = False
    task_payload = {
        "title": task.title,
        "description": task.description,
        "validation_criteria": task.validation_criteria,
    }
    mentioned_files = extract_mentioned_files(task_payload)

    # First try commit-based diff if task has linked commits. The linked
    # commits are the authoritative implementation artifact; changes_summary
    # is only supplemental prose.
    if task.commits:
        try:
            raw_diff, first_page = collect_task_diff_text(
                task_id=task.id,
                task_manager=task_manager,
                include_uncommitted=False,
                cwd=repo_path,
            )
            if raw_diff:
                changed_files = changed_files_from_diff(raw_diff)
                file_context_text = _read_referenced_file_context(
                    mentioned_files=mentioned_files,
                    changed_files=changed_files,
                    repo_path=repo_path,
                    max_chars=VALIDATION_FILE_CONTEXT_BUDGET_CHARS,
                )
                diff_budget = VALIDATION_PROMPT_BUDGET_CHARS
                if file_context_text:
                    diff_budget -= VALIDATION_FILE_CONTEXT_BUDGET_CHARS
                evidence = build_diff_validation_evidence(
                    raw_diff,
                    max_chars=diff_budget,
                    priority_files=mentioned_files,
                    agent_summary=changes_summary,
                )
                is_documentation_only = bool(evidence.manifest) and all(
                    Path(item.path).suffix.lower() in DOC_EXTENSIONS for item in evidence.manifest
                )
                changes_summary_included = evidence.agent_summary_included
                logger.info(
                    "Validation diff for task %s: raw_diff_chars=%d diff_chars=%d "
                    "file_context_chars=%d",
                    task.id,
                    len(raw_diff),
                    len(evidence.text),
                    len(file_context_text or ""),
                )
                validation_context = (
                    f"Commit-based diff ({first_page['commits']['total']} commits, "
                    f"{first_page['manifest']['total']} manifest entries):\n\n{evidence.text}"
                )
            else:
                logger.warning(
                    "diff pager returned empty for task %s with commits %s", task.id, task.commits
                )
        except Exception as e:
            logger.warning("diff pager failed for task %s: %s", task.id, e)

    if validation_context:
        if changes_summary and not changes_summary_included:
            validation_context = (
                f"{validation_context}\n\nAgent changes summary:\n{changes_summary}"
            )
    elif changes_summary:
        validation_context = changes_summary

    # Fall back to smart context ONLY if no linked commits.
    if not validation_context and not task.commits:
        from gobby.tasks.validation import get_validation_context_smart

        smart_context = get_validation_context_smart(
            task_title=task.title,
            validation_criteria=task.validation_criteria,
            task_description=task.description,
            cwd=repo_path,
        )
        if smart_context:
            validation_context = f"Validation context:\n\n{smart_context}"

    return ValidationContext(
        validation_context=validation_context,
        raw_diff=raw_diff,
        file_context_text=file_context_text,
        is_documentation_only=is_documentation_only,
    )


async def _recall_validation_lessons(
    ctx: "RegistryContext",
) -> tuple[str, list[dict[str, str]]]:
    service = getattr(ctx, "review_learning_service", None)
    if service is None:
        return "", []

    try:
        recall = await service.recall_review_lessons_by_class(
            lesson_domain="code",
            lesson_types=["validation-miss"],
            limit=3,
        )
        message = recall.get("message", "")
        if not isinstance(message, str):
            raise ValueError("review-learning recall returned a non-string message")
        return message, []
    except (AttributeError, ValueError, RuntimeError, OSError) as exc:
        detail = str(exc)
        logger.warning("Validation lesson recall failed: %s", detail, exc_info=True)
        return "", [
            {
                "code": "lesson-recall-failed",
                "severity": "warning",
                "detail": detail,
            }
        ]


def _with_validation_diagnostics(
    extra: dict[str, Any],
    diagnostics: list[dict[str, str]],
) -> dict[str, Any]:
    if not diagnostics:
        return extra
    return {**extra, "diagnostics": diagnostics}


async def validate_leaf_task_with_llm(
    task: Task,
    task_validator: "TaskValidator",
    validation_context: str,
    ctx: "RegistryContext",
    resolved_id: str,
    validation_config: "TaskValidationConfig | None",
    file_context_text: str | None = None,
    *,
    verification_receipt_text: str | None = None,
    admissible_evidence_ids: list[str] | None = None,
    read_only: bool = False,
) -> ValidationResult:
    """Run LLM validation on a leaf task.

    Args:
        task: The task to validate
        task_validator: The validator instance
        validation_context: Context for validation
        ctx: Registry context
        resolved_id: Resolved task ID
        validation_config: Validation configuration

    Returns:
        ValidationResult indicating if task can be closed
    """
    # Skip the LLM call entirely while an infrastructure-failure backoff is active,
    # so a generation outage does not re-run validation every heartbeat.
    backoff_store = TaskValidationBackoffStore(ctx.task_manager.db)
    now = utc_now()
    backoff_state = backoff_store.get(task.id)
    if backoff_state is not None and backoff_state.is_in_backoff_window(now):
        failure_category = classify_failure(
            backoff_state.last_error,
            default=FailureCategory.PROVIDER,
        )
        retry_at = (
            backoff_state.next_retry_at.isoformat() if backoff_state.next_retry_at else "later"
        )
        logger.warning(
            "Skipping validation for task %s: infrastructure backoff active "
            "(consecutive_failures=%d, retry after %s)",
            resolved_id,
            backoff_state.consecutive_failures,
            retry_at,
        )
        return ValidationResult(
            can_close=False,
            error_type="validation_infrastructure_unavailable",
            message=f"Validation generation unavailable (infrastructure); retry after {retry_at}.",
            extra={
                "validation_status": "error",
                "failure_category": failure_category.value,
                "retryable": True,
                "next_retry_at": retry_at,
                "consecutive_failures": backoff_state.consecutive_failures,
            },
            failure_category=failure_category,
        )

    lessons_section, recall_diagnostics = await _recall_validation_lessons(ctx)

    # Run LLM validation
    result = await task_validator.validate_task(
        task_id=task.id,
        title=task.title,
        description=task.description,
        changes_summary=validation_context,
        validation_criteria=task.validation_criteria,
        category=task.category,
        file_context_text=file_context_text,
        verification_receipt_text=verification_receipt_text,
        admissible_evidence_ids=admissible_evidence_ids or [],
        lessons_section=lessons_section,
    )
    result.diagnostics.extend(recall_diagnostics)

    # An LLM infrastructure failure (no candidate produced a usable result) is not a
    # verdict: record/extend the backoff and escalate after too many in a row, but do
    # not persist 'invalid' or burn the validation-failure / work-attempt counters.
    if result.status == "error":
        failure_category = result.failure_category or FailureCategory.PROVIDER
        if read_only:
            return ValidationResult(
                can_close=False,
                error_type="validation_infrastructure_unavailable",
                message=result.feedback,
                extra=_with_validation_diagnostics(
                    {
                        "validation_status": "error",
                        "failure_category": failure_category.value,
                        "retryable": True,
                    },
                    result.diagnostics,
                ),
                failure_category=failure_category,
            )
        state = backoff_store.record_failure(task.id, error=result.feedback, now=now)
        ctx.task_manager.update_task(
            resolved_id,
            validation_status="error",
            validation_feedback=result.feedback,
        )
        _record_validation_iteration(
            task,
            ctx,
            status="error",
            feedback=result.feedback,
            context_type="validation_evidence_gate",
            failure_category=failure_category,
            issues=result.issues,
        )
        retry_at = state.next_retry_at.isoformat() if state.next_retry_at else "later"
        if state.should_escalate():
            escalation_reason = (
                "validation generation unavailable after "
                f"{state.consecutive_failures} consecutive infrastructure failures"
            )
            ctx.task_manager.escalate_task(
                resolved_id,
                reason=escalation_reason,
            )
            logger.error(
                "Escalating task %s: validation generation unavailable after %d consecutive "
                "infrastructure failures",
                resolved_id,
                state.consecutive_failures,
            )
            return ValidationResult(
                can_close=False,
                error_type="validation_infrastructure_unavailable",
                message=(
                    f"Validation generation unavailable after {state.consecutive_failures} "
                    "consecutive infrastructure failures; escalated for manual review."
                ),
                extra=_with_validation_diagnostics(
                    {
                        "validation_status": "error",
                        "failure_category": failure_category.value,
                        "retryable": False,
                        "escalated": True,
                        "consecutive_failures": state.consecutive_failures,
                    },
                    result.diagnostics,
                ),
                failure_category=failure_category,
            )
        logger.warning(
            "Validation infrastructure failure for task %s (consecutive_failures=%d); "
            "backing off until %s",
            resolved_id,
            state.consecutive_failures,
            retry_at,
        )
        return ValidationResult(
            can_close=False,
            error_type="validation_infrastructure_unavailable",
            message=f"Validation generation unavailable (infrastructure); retry after {retry_at}.",
            extra=_with_validation_diagnostics(
                {
                    "validation_status": "error",
                    "failure_category": failure_category.value,
                    "retryable": True,
                    "next_retry_at": retry_at,
                    "consecutive_failures": state.consecutive_failures,
                },
                result.diagnostics,
            ),
            failure_category=failure_category,
        )

    validation_status = result.status
    original_feedback = result.feedback

    if validation_status != "valid":
        blocking_reasons = list(result.blocking_reasons)
        result_failure_category = result.failure_category
        if result_failure_category is None:
            raise RuntimeError("Non-valid validation result is missing a failure category")
        failure_category = result_failure_category
        persisted_status = persisted_validation_status(validation_status, failure_category)
        message = format_close_validation_message(
            validation_status,
            original_feedback,
            blocking_reasons,
            result.verdict_override,
        )
        if persisted_status == "error":
            if read_only:
                preview_extra: dict[str, Any] = {
                    "validation_status": "error",
                    "failure_category": failure_category.value,
                    "retryable": True,
                }
                if blocking_reasons:
                    preview_extra["blocking_reasons"] = blocking_reasons
                return ValidationResult(
                    can_close=False,
                    error_type="validation_infrastructure_failure",
                    message=message,
                    extra=_with_validation_diagnostics(preview_extra, result.diagnostics),
                    failure_category=failure_category,
                )
            state = backoff_store.record_failure(task.id, error=message, now=now)
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
                context_type="validation_evidence_gate",
                failure_category=failure_category,
                issues=result.issues,
            )
            retry_at = state.next_retry_at.isoformat() if state.next_retry_at else "later"
            if state.should_escalate():
                escalation_reason = (
                    "validation generation unavailable after "
                    f"{state.consecutive_failures} consecutive infrastructure failures"
                )
                ctx.task_manager.escalate_task(
                    resolved_id,
                    reason=escalation_reason,
                )
                logger.error(
                    "Escalating task %s: validation generation unavailable after %d consecutive "
                    "infrastructure failures",
                    resolved_id,
                    state.consecutive_failures,
                )
                return ValidationResult(
                    can_close=False,
                    error_type="validation_infrastructure_failure",
                    message=message,
                    extra=_with_validation_diagnostics(
                        {
                            "validation_status": "error",
                            "failure_category": failure_category.value,
                            "retryable": False,
                            "escalated": True,
                            "consecutive_failures": state.consecutive_failures,
                        },
                        result.diagnostics,
                    ),
                    failure_category=failure_category,
                )
            return ValidationResult(
                can_close=False,
                error_type="validation_infrastructure_failure",
                message=message,
                extra=_with_validation_diagnostics(
                    {
                        "validation_status": "error",
                        "failure_category": failure_category.value,
                        "retryable": True,
                        "next_retry_at": retry_at,
                        "consecutive_failures": state.consecutive_failures,
                    },
                    result.diagnostics,
                ),
                failure_category=failure_category,
            )
        if read_only:
            preview_extra = {
                "validation_status": validation_status,
                "failure_category": failure_category.value,
                "criterion_results": [
                    criterion_result.to_dict() for criterion_result in result.criterion_results
                ],
            }
            if result.verdict_override is not None:
                preview_extra["verdict_override"] = result.verdict_override
            if blocking_reasons:
                preview_extra["blocking_reasons"] = blocking_reasons
            return ValidationResult(
                can_close=False,
                error_type="validation_failed",
                message=message,
                extra=_with_validation_diagnostics(preview_extra, result.diagnostics),
                failure_category=failure_category,
            )
        if backoff_state is not None:
            backoff_store.clear(task.id)
        _record_validation_iteration(
            task,
            ctx,
            status=validation_status,
            feedback=message,
            context_type="validation_evidence_gate",
            failure_category=failure_category,
            issues=result.issues,
        )
        threshold = (
            validation_config.close_validation_escalation_threshold
            if validation_config is not None
            else 5
        )
        escalation_reason = (
            f"close validation remained {validation_status} after reaching the "
            f"{threshold}-attempt threshold"
        )
        try:
            fail_count, escalated_now = ctx.task_manager.increment_validation_failure(
                resolved_id,
                expected_updated_at=task.updated_at,
                threshold=threshold,
                validation_status=validation_status,
                validation_feedback=message,
                escalation_reason=escalation_reason,
            )
        except TaskStaleStateError as exc:
            return ValidationResult(
                can_close=False,
                error_type="stale_task_state",
                message=str(exc),
                extra=_with_validation_diagnostics(
                    {"validation_status": validation_status, "stale_state": True},
                    result.diagnostics,
                ),
            )

        extra: dict[str, Any] = {
            "validation_status": validation_status,
            "validation_fail_count": fail_count,
            "failure_category": failure_category.value,
            "criterion_results": [
                criterion_result.to_dict() for criterion_result in result.criterion_results
            ],
        }
        if result.verdict_override is not None:
            extra["verdict_override"] = result.verdict_override
        if escalated_now:
            from gobby.utils.session_context import get_current_session_id

            escalated = ctx.task_manager.get_task(resolved_id)
            event_id = coordinate_task_escalation(
                ctx,
                escalated,
                prior_owner_session_id=get_claimed_session_id(task),
                session_id=get_current_session_id(),
            )
            extra.update({"escalated": True, "escalation_event_id": event_id})
        if blocking_reasons:
            extra["blocking_reasons"] = blocking_reasons

        # Block closing on invalid or pending (error during validation)
        return ValidationResult(
            can_close=False,
            error_type="validation_failed",
            message=message,
            extra=_with_validation_diagnostics(extra, result.diagnostics),
            failure_category=failure_category,
        )

    # A real valid verdict clears any prior infrastructure outage state.
    recurring_validation_candidates: list[dict[str, Any]] = []
    if not read_only:
        if backoff_state is not None:
            backoff_store.clear(task.id)
        passing_iteration = _record_validation_iteration(
            task,
            ctx,
            status=validation_status,
            feedback=original_feedback,
            context_type="validation_evidence_gate",
            issues=result.issues,
        )
        configured = validation_config or TaskValidationConfig()
        summary = ValidationHistoryManager(ctx.task_manager.db).get_recurring_issue_summary(
            task.id,
            threshold=configured.recurring_issue_threshold,
            similarity_threshold=configured.issue_similarity_threshold,
        )
        passing_evidence = {
            "iteration": passing_iteration,
            "status": "valid",
            "feedback": original_feedback,
        }
        recurring_validation_candidates = [
            {**issue, "passing_iteration": passing_evidence}
            for issue in summary["recurring_issues"]
            if issue["anchors"]
        ]
    return ValidationResult(
        can_close=True,
        extra=_with_validation_diagnostics(
            {
                "recurring_validation_candidates": recurring_validation_candidates,
                "criterion_results": [
                    criterion_result.to_dict() for criterion_result in result.criterion_results
                ],
            },
            result.diagnostics,
        ),
        validation_status="valid",
        validation_feedback=original_feedback,
        reset_reason="llm_valid",
    )


def determine_close_outcome(
    task: Task,
    skip_validation: bool,
    override_justification: str | None,
) -> tuple[bool, bool]:
    """Determine the close outcome for a task.

    Args:
        task: The task being closed
        skip_validation: Whether validation was skipped
        override_justification: Justification for override

    Returns:
        Tuple of (route_to_escalation, store_override)
    """
    store_override = skip_validation

    # close_task enforces evidence-backed overrides directly, then closes with
    # validation_override_reason audit metadata. It does not route overrides to
    # escalation.
    return False, store_override
