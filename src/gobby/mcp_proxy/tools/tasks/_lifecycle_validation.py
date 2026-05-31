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
_SAME_SENTENCE_PROXIMITY = r"[^.!?]{0,100}"
_ZERO_FAILURE_TOKEN_RE = re.compile(
    r"\b(?:0\s+fail(?:ed|ures?)|zero\s+failures?|fail(?:ed|ures?)\s*[=:]\s*0)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_QUOTED_FEEDBACK_FRAGMENT_RE = re.compile(
    r"(?:\"[^\"]{1,240}\"|`[^`]{1,240}`|(?<!\w)'[^']{1,240}'(?!\w))",
    _FAILURE_FEEDBACK_FLAGS,
)
_NONZERO_FAILURE_COUNT_RE = re.compile(
    # Example: "1 failed" or "2 failures".
    r"\b(?:[1-9]\d*|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"fail(?:ed|ures?)\b|\bfail(?:ed|ures?)\s*[=:]\s*[1-9]\d*\b",
    _FAILURE_FEEDBACK_FLAGS,
)

_ACCEPTANCE_CRITERIA_THEN_FAILURE_RE = re.compile(
    # Example: "Acceptance criteria failed for the delivered implementation."
    r"\b(?:acceptance\s+)?criteri(?:on|a)\b.{0,80}"
    r"\b(?:failed|failing|unmet|unsatisfied|not\s+(?:satisfied|met))\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_FAILURE_THEN_ACCEPTANCE_CRITERIA_RE = re.compile(
    # Example: "Failed acceptance criteria remain unresolved."
    r"\b(?:failed|failing|unmet|unsatisfied|not\s+(?:satisfied|met))\b.{0,80}"
    r"\b(?:acceptance\s+)?criteri(?:on|a)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_VALIDATION_GATE_THEN_FAILURE_RE = re.compile(
    # Example: "Required validation gate did not pass."
    rf"\b{_VALIDATION_GATE_WORDS}\b{_SAME_SENTENCE_PROXIMITY}\b{_VALIDATION_FAILURE_WORDS}\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_FAILURE_THEN_VALIDATION_GATE_RE = re.compile(
    # Example: "Tests are failing in the required validation check."
    rf"\b{_VALIDATION_FAILURE_WORDS}\b{_SAME_SENTENCE_PROXIMITY}\b{_VALIDATION_GATE_WORDS}\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_VALIDATION_GATE_THEN_ERRORS_REMAIN_RE = re.compile(
    # Example: "Validation gate errors remain unresolved."
    rf"\b{_VALIDATION_GATE_WORDS}\b.{{0,100}}\berrors?\b.{{0,40}}"
    r"\b(?:remain|remaining|unresolved)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_VALIDATION_ERRORS_REMAIN_RE = re.compile(
    # Example: "Validation errors remain in the package."
    r"\b(?:validation|verification)\s+errors?\b.{0,40}"
    r"\b(?:remain|remaining|unresolved)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_ERRORS_REMAIN_THEN_VALIDATION_GATE_RE = re.compile(
    # Example: "Errors remain in the validation step."
    r"\berrors?\b.{0,40}\b(?:remain|remaining|unresolved)\b.{0,100}"
    rf"\b{_VALIDATION_GATE_WORDS}\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_ERRORS_PREVENTED_CLEAN_PASS_RE = re.compile(
    # Example: "Errors prevented a clean pass."
    r"\berrors?\b.{0,80}\bprevented\b.{0,80}\b(?:clean|pass(?:ing)?|valid)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_REMAINING_GAP_IS_VALIDATION_RE = re.compile(
    # Example: "The only gap is the coverage gate."
    r"\b(?:only|remaining)\s+gap\s+(?:is|remains)\b.{0,120}"
    rf"\b(?:{_VALIDATION_GATE_WORDS}|criteri(?:on|a))\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_MYPY_THEN_INCOMPLETE_RE = re.compile(
    # Example: "mypy is incomplete at the service boundary."
    r"\bmypy\b.{0,80}\b(?:incomplete|unresolved)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_INCOMPLETE_THEN_MYPY_RE = re.compile(
    # Example: "Incomplete mypy work remains."
    r"\b(?:incomplete|unresolved)\b.{0,80}\bmypy\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_REQUIRED_FAILURE_FEEDBACK_PATTERNS: tuple[re.Pattern[str], ...] = (
    _NONZERO_FAILURE_COUNT_RE,
    _ACCEPTANCE_CRITERIA_THEN_FAILURE_RE,
    _FAILURE_THEN_ACCEPTANCE_CRITERIA_RE,
    _VALIDATION_GATE_THEN_FAILURE_RE,
    _FAILURE_THEN_VALIDATION_GATE_RE,
    _VALIDATION_GATE_THEN_ERRORS_REMAIN_RE,
    _VALIDATION_ERRORS_REMAIN_RE,
    _ERRORS_REMAIN_THEN_VALIDATION_GATE_RE,
    _ERRORS_PREVENTED_CLEAN_PASS_RE,
    _REMAINING_GAP_IS_VALIDATION_RE,
    _MYPY_THEN_INCOMPLETE_RE,
    _INCOMPLETE_THEN_MYPY_RE,
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
    return matched_required_validation_failure_pattern(feedback) is not None


_SUCCESSFUL_VALIDATION_FEEDBACK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?<!\bnot\s)\ball\s+(?:\w+\s+){0,6}"
        r"(?:validation\s+criteria|acceptance\s+criteria|criteria)\s+"
        r"(?:are\s+|were\s+)?(?:satisfied|met|passed)\b",
        re.IGNORECASE,
    ),
)


def matched_successful_validation_pattern(feedback: str | None) -> re.Pattern[str] | None:
    """Return the validation-success pattern matched by feedback, if any."""
    if not feedback or matched_required_validation_failure_pattern(feedback) is not None:
        return None

    normalized_feedback = _ZERO_FAILURE_TOKEN_RE.sub("", " ".join(feedback.split()))
    searchable_feedback = _QUOTED_FEEDBACK_FRAGMENT_RE.sub("", normalized_feedback)
    for pattern in _SUCCESSFUL_VALIDATION_FEEDBACK_PATTERNS:
        if pattern.search(searchable_feedback) is not None:
            return pattern
    return None


def matched_required_validation_failure_pattern(feedback: str | None) -> re.Pattern[str] | None:
    """Return the validation-failure pattern matched by feedback, if any."""
    if not feedback:
        return None

    normalized_feedback = _ZERO_FAILURE_TOKEN_RE.sub("", " ".join(feedback.split()))
    searchable_feedback = _QUOTED_FEEDBACK_FRAGMENT_RE.sub("", normalized_feedback)
    for pattern in _REQUIRED_FAILURE_FEEDBACK_PATTERNS:
        if pattern.search(searchable_feedback) is not None:
            return pattern
    return None


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

    validation_context = ""
    raw_diff = None

    # First try commit-based diff if task has linked commits. The linked
    # commits are the authoritative implementation artifact; changes_summary
    # is only supplemental prose.
    if task.commits:
        try:
            diff_result = get_task_diff(
                task_id=task.id,
                task_manager=task_manager,
                include_uncommitted=False,
                cwd=repo_path,
            )
            if diff_result.diff:
                raw_diff = diff_result.diff
                summarized_diff = summarize_diff_for_validation(raw_diff)
                validation_context = (
                    f"Commit-based diff ({len(diff_result.commits)} commits, "
                    f"{diff_result.file_count} files):\n\n{summarized_diff}"
                )
            else:
                logger.warning(
                    f"get_task_diff returned empty for task {task.id} with commits {task.commits}"
                )
        except Exception as e:
            logger.warning(f"get_task_diff failed for task {task.id}: {e}")

    if validation_context and changes_summary:
        validation_context = f"{validation_context}\n\nAgent changes summary:\n{changes_summary}"
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
    matched_failure_pattern = matched_required_validation_failure_pattern(result.feedback)
    matched_success_pattern = matched_successful_validation_pattern(result.feedback)
    if result.status == "valid" and matched_failure_pattern is not None:
        logger.warning(
            "Overriding validation status for task %s: LLM returned 'valid' but feedback "
            "admits failure. Pattern: %s. Feedback: %s",
            resolved_id,
            matched_failure_pattern.pattern,
            result.feedback,
        )
        validation_status = "invalid"
    elif result.status == "invalid" and matched_success_pattern is not None:
        logger.warning(
            "Overriding validation status for task %s: LLM returned %r but feedback "
            "says validation criteria are satisfied. Pattern: %s. Feedback: %s",
            resolved_id,
            result.status,
            matched_success_pattern.pattern,
            result.feedback,
        )
        validation_status = "valid"

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
    store_override = skip_validation

    # close_task enforces evidence-backed overrides directly, then closes with
    # validation_override_reason audit metadata. It does not route overrides to
    # escalation.
    return False, store_override
