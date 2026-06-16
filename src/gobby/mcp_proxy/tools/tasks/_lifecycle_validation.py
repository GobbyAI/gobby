"""Validation helpers for task lifecycle operations.

Provides validation functions used by close_task to verify tasks
can be closed (commit checks, child completion, LLM validation).
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.tasks._helpers import SKIP_REASONS
from gobby.storage.tasks import Task
from gobby.storage.tasks._validation_backoff import TaskValidationBackoffStore
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
# Keep failure words close to the validation term so broad feedback does not
# turn unrelated failures elsewhere in the paragraph into close-blocking gates.
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
    r"\b(?:validation|verification)\s+errors?\s+"
    r"(?:remain|remaining|(?:are|is)\s+unresolved|unresolved)\b",
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


@dataclass(frozen=True)
class ValidationContext:
    """Evidence packet passed from close_task into LLM validation."""

    validation_context: str | None
    raw_diff: str | None
    file_context_text: str | None = None


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

    base_path = Path(repo_path) if repo_path else Path.cwd()
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
            if candidate.is_file() and candidate not in resolved:
                resolved.append(candidate)

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


def feedback_admits_required_validation_failure(feedback: str | None) -> bool:
    """Return True when validator feedback explicitly admits a required gate failed."""
    return matched_required_validation_failure_pattern(feedback) is not None


_SUCCESSFUL_VALIDATION_FEEDBACK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:(?=.*\b(?:fixed|resolved|verified|re-?tested)\b).*?)?"
        r"(?<!\bnot\s)\ball\s+"
        r"(?:(?!(?:previous|previously|prior|unmet|unsatisfied)\b)\w+\s+){0,3}"
        r"(?:validation\s+criteria|acceptance\s+criteria)\s+"
        r"(?:are\s+|were\s+)?(?:satisfied|met|passed)\b",
        re.IGNORECASE,
    ),
)


def matched_successful_validation_pattern(feedback: str | None) -> re.Pattern[str] | None:
    """Return the validation-success pattern matched by feedback, if any."""
    if not feedback or matched_required_validation_failure_pattern(feedback) is not None:
        return None

    return _matched_successful_validation_pattern_unchecked(feedback)


def _matched_successful_validation_pattern_unchecked(
    feedback: str | None,
) -> re.Pattern[str] | None:
    """Return a success match without applying failure-precedence filtering."""
    if not feedback:
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
        changed_files_from_diff,
        extract_mentioned_files,
        get_task_diff,
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
            diff_result = get_task_diff(
                task_id=task.id,
                task_manager=task_manager,
                include_uncommitted=False,
                cwd=repo_path,
            )
            if diff_result.diff:
                raw_diff = diff_result.diff
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
                changes_summary_included = bool(changes_summary)
                logger.info(
                    "Validation diff for task %s: raw_diff_chars=%d diff_chars=%d "
                    "file_context_chars=%d",
                    task.id,
                    len(raw_diff),
                    len(evidence.text),
                    len(file_context_text or ""),
                )
                validation_context = (
                    f"Commit-based diff ({len(diff_result.commits)} commits, "
                    f"{diff_result.file_count} files):\n\n{evidence.text}"
                )
            else:
                logger.warning(
                    f"get_task_diff returned empty for task {task.id} with commits {task.commits}"
                )
        except Exception as e:
            logger.warning(f"get_task_diff failed for task {task.id}: {e}")

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
    )


async def validate_leaf_task_with_llm(
    task: Task,
    task_validator: "TaskValidator",
    validation_context: str,
    raw_diff: str | None,
    ctx: "RegistryContext",
    resolved_id: str,
    validation_config: "TaskValidationConfig | None",
    file_context_text: str | None = None,
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

    # Skip the LLM call entirely while an infrastructure-failure backoff is active,
    # so a generation outage does not re-run validation every heartbeat.
    backoff_store = TaskValidationBackoffStore(ctx.task_manager.db)
    now = datetime.now(UTC)
    backoff_state = backoff_store.get(task.id)
    if backoff_state is not None and backoff_state.is_in_backoff_window(now):
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
                "retryable": True,
                "next_retry_at": retry_at,
                "consecutive_failures": backoff_state.consecutive_failures,
            },
        )

    # Run LLM validation
    result = await task_validator.validate_task(
        task_id=task.id,
        title=task.title,
        description=task.description,
        changes_summary=validation_context,
        validation_criteria=task.validation_criteria,
        category=task.category,
        file_context_text=file_context_text,
    )

    # An LLM infrastructure failure (no candidate produced a usable result) is not a
    # verdict: record/extend the backoff and escalate after too many in a row, but do
    # not persist 'invalid' or burn the validation-failure / work-attempt counters.
    if result.status == "error":
        state = backoff_store.record_failure(task.id, error=result.feedback, now=now)
        ctx.task_manager.update_task(
            resolved_id,
            validation_status="error",
            validation_feedback=result.feedback,
        )
        retry_at = state.next_retry_at.isoformat() if state.next_retry_at else "later"
        if state.should_escalate():
            ctx.task_manager.update_task(
                resolved_id,
                escalated_at=now.isoformat(),
                escalation_reason=(
                    "validation generation unavailable after "
                    f"{state.consecutive_failures} consecutive infrastructure failures"
                ),
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
                extra={
                    "validation_status": "error",
                    "retryable": False,
                    "escalated": True,
                    "consecutive_failures": state.consecutive_failures,
                },
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
            extra={
                "validation_status": "error",
                "retryable": True,
                "next_retry_at": retry_at,
                "consecutive_failures": state.consecutive_failures,
            },
        )

    # Real verdict (or pending/disabled) this round — clear any prior infra backoff so
    # an old outage cannot poison later attempts.
    if backoff_state is not None:
        backoff_store.clear(task.id)

    validation_status = result.status
    original_feedback = result.feedback
    matched_failure_pattern = matched_required_validation_failure_pattern(original_feedback)
    matched_success_pattern = _matched_successful_validation_pattern_unchecked(original_feedback)
    feedback_length = len(original_feedback or "")
    if matched_failure_pattern is not None and matched_success_pattern is not None:
        logger.warning(
            "Validation feedback for task %s contains both failure and "
            "success evidence; failure takes precedence. Failure pattern: %s. Success "
            "pattern: %s. Status: %s. Feedback length: %d",
            resolved_id,
            matched_failure_pattern.pattern,
            matched_success_pattern.pattern,
            result.status,
            feedback_length,
        )
        if result.status != "pending":
            validation_status = "invalid"
    elif result.status == "valid" and matched_failure_pattern is not None:
        logger.warning(
            "Overriding validation status for task %s: LLM returned 'valid' but feedback "
            "admits failure. Pattern: %s. Status: %s. Feedback length: %d",
            resolved_id,
            matched_failure_pattern.pattern,
            result.status,
            feedback_length,
        )
        validation_status = "invalid"
    elif result.status == "invalid" and matched_success_pattern is not None:
        logger.warning(
            "Overriding validation status for task %s: LLM returned %r but feedback "
            "says validation criteria are satisfied. Pattern: %s. Feedback length: %d",
            resolved_id,
            result.status,
            matched_success_pattern.pattern,
            feedback_length,
        )
        validation_status = "valid"

    # Store validation result regardless of pass/fail
    ctx.task_manager.update_task(
        resolved_id,
        validation_status=validation_status,
        validation_feedback=original_feedback,
    )

    if validation_status != "valid":
        # Block closing on invalid or pending (error during validation)
        return ValidationResult(
            can_close=False,
            error_type="validation_failed",
            message=original_feedback or "Validation did not pass",
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
