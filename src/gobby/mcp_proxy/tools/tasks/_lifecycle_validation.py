"""Validation helpers for task lifecycle operations.

Provides validation functions used by close_task to verify tasks
can be closed (commit checks, child completion, LLM validation).
"""

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.tasks._helpers import SKIP_REASONS
from gobby.storage.tasks import Task
from gobby.tasks.state_semantics import is_task_closed

if TYPE_CHECKING:
    from gobby.config.tasks import TaskValidationConfig
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.storage.tasks import LocalTaskManager
    from gobby.tasks.validation import TaskValidator

logger = logging.getLogger(__name__)

_FAILURE_FEEDBACK_FLAGS = re.IGNORECASE | re.DOTALL
_VALIDATION_GATE_WORDS = (
    r"(?:"
    r"(?:required\s+)?(?:validation|verification|quality)\s+(?:gate|check|step)s?|"
    r"(?:required\s+)?checks?|"
    r"(?:test|build|compil(?:e|ation|er)|lint|format|coverage|static\s+analysis)"
    r"(?:\s+(?:gate|check|step))?s?|"
    r"ci(?:\s+(?:gate|check|step))?"
    r")"
)
_VALIDATION_FAILURE_WORDS = r"(?:failed|failing|not\s+clean|did\s+not\s+pass|not\s+pass(?:ed|ing)?)"
_REQUIRED_FAILURE_FEEDBACK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:acceptance\s+)?criteri(?:on|a)\b.{0,80}"
        r"\b(?:failed|failing|unmet|unsatisfied|not\s+(?:satisfied|met))\b",
        _FAILURE_FEEDBACK_FLAGS,
    ),
    re.compile(
        r"\b(?:failed|failing|unmet|unsatisfied|not\s+(?:satisfied|met))\b.{0,80}"
        r"\b(?:acceptance\s+)?criteri(?:on|a)\b",
        _FAILURE_FEEDBACK_FLAGS,
    ),
    re.compile(
        rf"\b{_VALIDATION_GATE_WORDS}\b.{{0,100}}\b{_VALIDATION_FAILURE_WORDS}\b",
        _FAILURE_FEEDBACK_FLAGS,
    ),
    re.compile(
        rf"\b{_VALIDATION_FAILURE_WORDS}\b.{{0,100}}\b{_VALIDATION_GATE_WORDS}\b",
        _FAILURE_FEEDBACK_FLAGS,
    ),
    re.compile(
        rf"\b{_VALIDATION_GATE_WORDS}\b.{{0,100}}\berrors?\b.{{0,40}}"
        r"\b(?:remain|remaining|unresolved)\b",
        _FAILURE_FEEDBACK_FLAGS,
    ),
    re.compile(
        r"\b(?:validation|verification)\s+errors?\b.{0,40}"
        r"\b(?:remain|remaining|unresolved)\b",
        _FAILURE_FEEDBACK_FLAGS,
    ),
    re.compile(
        r"\berrors?\b.{0,40}\b(?:remain|remaining|unresolved)\b.{0,100}"
        rf"\b{_VALIDATION_GATE_WORDS}\b",
        _FAILURE_FEEDBACK_FLAGS,
    ),
    re.compile(
        r"\berrors?\b.{0,80}\bprevented\b.{0,80}\b(?:clean|pass(?:ing)?|valid)\b",
        _FAILURE_FEEDBACK_FLAGS,
    ),
    re.compile(
        r"\b(?:only|remaining)\s+gap\s+(?:is|remains)\b.{0,120}"
        rf"\b(?:{_VALIDATION_GATE_WORDS}|criteri(?:on|a))\b",
        _FAILURE_FEEDBACK_FLAGS,
    ),
    re.compile(
        r"\bmypy\b.{0,80}\b(?:incomplete|unresolved)\b",
        _FAILURE_FEEDBACK_FLAGS,
    ),
    re.compile(
        r"\b(?:incomplete|unresolved)\b.{0,80}\bmypy\b",
        _FAILURE_FEEDBACK_FLAGS,
    ),
)


@dataclass
class ValidationResult:
    """Result of validation checks."""

    can_close: bool
    error_type: str | None = None
    message: str | None = None
    extra: dict[str, Any] | None = None


def feedback_admits_required_validation_failure(feedback: str | None) -> bool:
    """Return True when validator feedback explicitly admits a required gate failed."""
    if not feedback:
        return False

    normalized_feedback = " ".join(feedback.split())
    return any(
        pattern.search(normalized_feedback) is not None
        for pattern in _REQUIRED_FAILURE_FEEDBACK_PATTERNS
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
    children = ctx.task_manager.list_tasks(parent_task_id=task_id, limit=1000)

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
) -> tuple[str | None, str | None]:
    """Gather context for LLM validation.

    Uses provided changes_summary or auto-fetches via smart context gathering.

    Args:
        task: The task to validate
        changes_summary: Optional user-provided summary
        repo_path: Path to the repository
        task_manager: LocalTaskManager for fetching task diff

    Returns:
        Tuple of (validation_context, raw_diff)
    """
    from gobby.tasks.commits import get_task_diff, summarize_diff_for_validation

    validation_context = changes_summary
    raw_diff = None

    if not validation_context:
        # First try commit-based diff if task has linked commits
        if task.commits:
            try:
                # Don't include uncommitted changes - they're likely unrelated to this task
                # The linked commits ARE the work for this task
                diff_result = get_task_diff(
                    task_id=task.id,
                    task_manager=task_manager,
                    include_uncommitted=False,
                    cwd=repo_path,
                )
                if diff_result.diff:
                    raw_diff = diff_result.diff
                    # Use smart summarization to ensure all files are visible
                    summarized_diff = summarize_diff_for_validation(raw_diff)
                    validation_context = (
                        f"Commit-based diff ({len(diff_result.commits)} commits, "
                        f"{diff_result.file_count} files):\n\n{summarized_diff}"
                    )
                else:
                    logger.warning(
                        f"get_task_diff returned empty for task {task.id} "
                        f"with commits {task.commits}"
                    )
            except Exception as e:
                logger.warning(f"get_task_diff failed for task {task.id}: {e}")

        # Fall back to smart context ONLY if no linked commits
        # (uncommitted changes are unrelated if we have specific commits linked)
        if not validation_context and not task.commits:
            from gobby.tasks.validation import get_validation_context_smart

            # Smart context gathering: uncommitted changes + multi-commit window + file analysis
            smart_context = get_validation_context_smart(
                task_title=task.title,
                validation_criteria=task.validation_criteria,
                task_description=task.description,
                cwd=repo_path,
            )
            if smart_context:
                validation_context = f"Validation context:\n\n{smart_context}"

    return validation_context, raw_diff


async def validate_leaf_task_with_llm(
    task: Task,
    task_validator: "TaskValidator",
    validation_context: str,
    raw_diff: str | None,
    ctx: "RegistryContext",
    resolved_id: str,
    validation_config: "TaskValidationConfig | None",
) -> ValidationResult:
    """Run LLM validation on a leaf task.

    Args:
        task: The task to validate
        task_validator: The validator instance
        validation_context: Context for validation
        raw_diff: Raw diff for doc-only check
        ctx: Registry context
        resolved_id: Resolved task ID
        validation_config: Validation configuration

    Returns:
        ValidationResult indicating if task can be closed
    """
    from gobby.tasks.commits import is_doc_only_diff

    # Auto-skip LLM validation for doc-only changes
    if raw_diff and is_doc_only_diff(raw_diff):
        logger.info(f"Skipping LLM validation for task {task.id}: doc-only changes")
        ctx.task_manager.update_task(
            resolved_id,
            validation_status="valid",
            validation_feedback="Auto-validated: documentation-only changes",
        )
        return ValidationResult(can_close=True)

    # Run LLM validation
    result = await task_validator.validate_task(
        task_id=task.id,
        title=task.title,
        description=task.description,
        changes_summary=validation_context,
        validation_criteria=task.validation_criteria,
        category=task.category,
    )

    validation_status = result.status
    if result.status == "valid" and feedback_admits_required_validation_failure(result.feedback):
        logger.warning(
            "Overriding validation status for task %s: LLM returned 'valid' but feedback "
            "admits failure. Feedback: %s",
            resolved_id,
            result.feedback,
        )
        validation_status = "invalid"

    # Store validation result regardless of pass/fail
    ctx.task_manager.update_task(
        resolved_id,
        validation_status=validation_status,
        validation_feedback=result.feedback,
    )

    if validation_status != "valid":
        # Block closing on invalid or pending (error during validation)
        return ValidationResult(
            can_close=False,
            error_type="validation_failed",
            message=result.feedback or "Validation did not pass",
            extra={"validation_status": validation_status},
        )

    return ValidationResult(can_close=True)


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
    # Determine if override should be stored
    store_override = skip_validation

    # Route to escalation if override was used — skipped validation needs explicit human triage
    route_to_escalation = bool(override_justification and store_override)

    return route_to_escalation, store_override
