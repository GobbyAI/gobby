"""Small validation helpers for the task-close checklist."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from gobby.config.tasks import TaskValidationConfig
from gobby.failure_categories import FailureCategory
from gobby.mcp_proxy.tools._task_query_pagination import collect_task_query_pages
from gobby.mcp_proxy.tools.tasks._escalation_coordinator import coordinate_task_escalation
from gobby.storage.tasks import Task, TaskAlreadyEscalatedError, TaskStaleStateError
from gobby.storage.tasks._validation_backoff import TaskValidationBackoffStore
from gobby.tasks.close_verdict import (
    FINDING_SEVERITY_ORDER,
    CloseVerdict,
)
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_closed
from gobby.tasks.validation_history import ValidationHistoryManager
from gobby.utils.daemon_git import normalize_commit_sha
from gobby.utils.datetime import utc_now
from gobby.workflows.commit_guard import (
    DirtyEditOwnershipInspectionError,
    foreign_owned_dirty_paths,
)
from gobby.workflows.task_dirty_state import task_dirty_paths_async

if TYPE_CHECKING:
    from collections.abc import Sequence
    from collections.abc import Set as AbstractSet

    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True, slots=True)
class TaskCleanProof:
    """Result of proving only the target task's attributed paths clean."""

    status: Literal["clean", "dirty", "unavailable", "skipped"]
    dirty_paths: frozenset[str] = frozenset()
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        details: dict[str, object] = {"status": self.status}
        if self.dirty_paths:
            details["dirty_paths"] = sorted(self.dirty_paths)
        if self.reason is not None:
            details["reason"] = self.reason
        return details


async def evaluate_task_clean_proof(
    ctx: RegistryContext,
    *,
    edited_paths: AbstractSet[str],
    repo_path: str,
) -> TaskCleanProof:
    """Prove target-attributed paths clean, or report why proof was skipped or unavailable."""
    if (
        ctx.validation_config is not None
        and not ctx.validation_config.require_clean_attributed_paths_on_close
    ):
        return TaskCleanProof(status="skipped", reason="disabled_by_configuration")

    dirty_paths = await task_dirty_paths_async(set(edited_paths), repo_path)
    if dirty_paths is None:
        return TaskCleanProof(status="unavailable", reason="git_status_unavailable")
    if dirty_paths:
        return TaskCleanProof(status="dirty", dirty_paths=frozenset(dirty_paths))
    return TaskCleanProof(status="clean")


async def apply_task_cleanliness_gate(
    ctx: RegistryContext,
    evaluation: CloseEvaluation,
    *,
    edited_paths: AbstractSet[str],
    owner_session_id: str,
    project_id: str,
    repo_path: str,
) -> None:
    """Apply target-attributed clean-path proof to the close checklist."""
    proof = await evaluate_task_clean_proof(
        ctx,
        edited_paths=edited_paths,
        repo_path=repo_path,
    )
    evaluation.extra["clean_proof"] = proof.as_dict()
    if proof.status == "skipped":
        evaluation.pass_gate(
            9,
            "uncommitted_task_edits",
            "Clean-path proof disabled by configuration.",
            skipped=True,
        )
        return
    if proof.status == "unavailable":
        evaluation.collect_failure(
            9,
            "uncommitted_task_edits",
            "task_clean_proof_unavailable",
            "Git could not prove that task-attributed files are clean. Retry after Git recovers.",
        )
        return

    dirty_result = await asyncio.to_thread(
        validate_uncommitted_task_edits,
        ctx,
        dirty_paths=proof.dirty_paths,
        owner_session_id=owner_session_id,
        project_id=project_id,
        repo_path=repo_path,
    )
    if dirty_result.can_close:
        evaluation.pass_gate(9, "uncommitted_task_edits", "No task-attributed files are dirty.")
        return
    evaluation.collect_failure(
        9,
        "uncommitted_task_edits",
        dirty_result.error_type or "uncommitted_task_edits",
        dirty_result.message or "Task-attributed files still have uncommitted changes.",
        details=dirty_result.extra,
    )


async def validate_commit_requirements(
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
        stale = [
            sha for sha in task.commits if await normalize_commit_sha(sha, cwd=repo_path) is None
        ]
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


def validate_uncommitted_task_edits(
    ctx: RegistryContext,
    *,
    dirty_paths: AbstractSet[str],
    owner_session_id: str,
    project_id: str,
    repo_path: str,
) -> ValidationResult:
    """Name every dirty task path and its active foreign owner, when present."""
    ordered_paths = sorted(dirty_paths)
    if not ordered_paths:
        return ValidationResult(can_close=True)

    try:
        foreign_owners = foreign_owned_dirty_paths(
            ctx.task_manager.db,
            session_id=owner_session_id,
            project_id=project_id,
            checkout_root=repo_path,
            paths=set(ordered_paths),
        )
    except DirtyEditOwnershipInspectionError:
        logger.warning(
            "Dirty-path ownership inspection failed during close_task; "
            "reporting dirty paths without owners",
            extra={"project_id": project_id, "session_id": owner_session_id},
            exc_info=True,
        )
        foreign_owners = {}

    owner_sessions = {
        path: owners[0].session_ref if (owners := foreign_owners.get(path)) else None
        for path in ordered_paths
    }
    rendered_paths = ", ".join(
        f"{path} (dirty by session {owner})" if owner else path
        for path, owner in owner_sessions.items()
    )
    return ValidationResult(
        can_close=False,
        error_type="uncommitted_task_edits",
        message=(
            f"Task-attributed files still have uncommitted changes: {rendered_paths}. "
            "Commit them, or ask the owner to commit or release_task_paths, and retry."
        ),
        extra={
            "dirty_paths": ordered_paths,
            "foreign_owner_sessions": owner_sessions,
        },
    )


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
    verdict_dict = verdict.to_dict()
    pending_external = [
        criterion.criterion
        for criterion in verdict.criteria
        if criterion.verdict_state == "pending_external"
    ]
    gap_verdicts = [criterion for criterion in verdict.criteria if criterion.verdict_state == "gap"]
    min_severity = validation_config.close_review_min_severity if validation_config else "low"
    blocking_findings = [
        finding
        for finding in verdict.findings
        if FINDING_SEVERITY_ORDER[finding.severity] >= FINDING_SEVERITY_ORDER[min_severity]
    ]
    if pending_external and not gap_verdicts and not blocking_findings:
        message = (
            "Implementation criteria passed; coordinator-owned live criteria remain pending: "
            f"{', '.join(pending_external)}"
        )
        ctx.task_manager.update_task(
            resolved_id,
            validation_status="pending",
            validation_feedback=message,
        )
        _record_validation_iteration(
            task,
            ctx,
            status="pending",
            feedback=message,
            failure_category=None,
        )
        return ValidationResult(
            can_close=False,
            error_type="external_pending",
            message=message,
            extra={
                "verdict": verdict_dict,
                "pending_external_criteria": pending_external,
                "blocking_reasons": [],
                "required_actions": [
                    "End this agent run; the coordinator must verify the pending Live criteria "
                    "and close the task."
                ],
            },
            validation_status="pending",
            validation_feedback=message,
        )

    if not gap_verdicts and not blocking_findings and verdict.valid:
        _record_validation_iteration(
            task,
            ctx,
            status="valid",
            feedback=verdict.feedback,
            failure_category=None,
        )
        return ValidationResult(
            can_close=True,
            extra={"verdict": verdict_dict},
            validation_status="valid",
            validation_feedback=verdict.feedback,
            reset_reason=reset_reason,
        )

    _record_validation_iteration(
        task,
        ctx,
        status="invalid",
        feedback=verdict.feedback,
        failure_category=FailureCategory.CODE,
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
    if pending_external:
        extra["pending_external_criteria"] = pending_external
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
        " ".join(
            part
            for part in (
                criterion.gap,
                (
                    f"Required evidence: {criterion.required_evidence}"
                    if criterion.required_evidence
                    else None
                ),
            )
            if part
        )
        for criterion in gap_verdicts
        if criterion.gap or criterion.required_evidence
    ]
    requirements = gaps or [verdict.feedback]
    requirements.extend(
        f"{finding.severity} {finding.category} finding at "
        f"{finding.path}:{finding.start_line}-{finding.end_line}: {finding.description}"
        for finding in blocking_findings
    )
    return ValidationResult(
        can_close=False,
        error_type="validation_failed",
        message=verdict.feedback,
        # The requirements are the blocking reasons; repeating them as
        # required_actions states the same sentences a second time.
        extra={**extra, "blocking_reasons": requirements},
        failure_category=FailureCategory.CODE,
        validation_status="invalid",
        validation_feedback=verdict.feedback,
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
    "record_validation_infrastructure_failure",
    "validate_commit_requirements",
    "validate_uncommitted_task_edits",
    "validate_parent_task",
]
