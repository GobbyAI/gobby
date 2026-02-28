"""
Task validation module.

Handles validating task completion against acceptance criteria
using LLM providers.

Multi-strategy context gathering:
1. Current uncommitted changes (staged + unstaged)
2. Multi-commit window (last N commits, configurable)
3. File-based analysis (read files mentioned in criteria)

TODO: Add strategy 4 - codebase grep for test files related to the task.
      Implementation location: get_validation_context_smart() after Strategy 3.
"""

import logging
import re
import subprocess  # nosec B404 - subprocess needed for validation commands
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMService
from gobby.prompts import PromptLoader
from gobby.storage.database import DatabaseProtocol
from gobby.utils.json_helpers import extract_json_object

logger = logging.getLogger(__name__)


# Default number of commits to look back when gathering context
DEFAULT_COMMIT_WINDOW = 10
DEFAULT_MAX_CHARS = 50000


def run_git_command(
    cmd: list[str],
    cwd: str | Path | None = None,
    timeout: int = 10,
) -> subprocess.CompletedProcess[str] | None:
    """Run git command with standardized exception handling.

    Returns CompletedProcess on success, None on exception (logs debug).
    Caller is responsible for checking returncode and processing stdout.

    Args:
        cmd: Git command as list of strings (e.g., ["git", "diff"])
        cwd: Working directory for the command
        timeout: Command timeout in seconds (default: 10)

    Returns:
        CompletedProcess on success, None if exception occurred
    """
    try:
        return subprocess.run(  # nosec B603 - cmd passed from internal callers with hardcoded git commands
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except Exception as e:
        logger.debug(f"Git command failed ({' '.join(cmd)}): {e}")
        return None


def get_last_commit_diff(
    max_chars: int = DEFAULT_MAX_CHARS,
    cwd: str | Path | None = None,
) -> str | None:
    """Get diff from the most recent commit.

    Args:
        max_chars: Maximum characters to return (truncates if larger)
        cwd: Working directory for git commands (project repo path)

    Returns:
        Diff string from HEAD~1..HEAD, or None if not available
    """
    result = run_git_command(["git", "diff", "HEAD~1..HEAD"], cwd=cwd)
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return None

    diff: str = result.stdout
    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n\n... [diff truncated] ..."

    return diff


def get_recent_commits(
    n: int = DEFAULT_COMMIT_WINDOW,
    cwd: str | Path | None = None,
) -> list[dict[str, str]]:
    """Get list of recent commits with SHA and subject.

    Args:
        n: Number of commits to retrieve
        cwd: Working directory for git commands (project repo path)

    Returns:
        List of dicts with 'sha' and 'subject' keys
    """
    result = run_git_command(["git", "log", f"-{n}", "--pretty=format:%H|%s"], cwd=cwd)
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return []

    commits = []
    for line in result.stdout.strip().split("\n"):
        if "|" in line:
            sha, subject = line.split("|", 1)
            commits.append({"sha": sha, "subject": subject})

    return commits


def get_multi_commit_diff(
    commit_count: int = DEFAULT_COMMIT_WINDOW,
    max_chars: int = DEFAULT_MAX_CHARS,
    cwd: str | Path | None = None,
) -> str | None:
    """Get combined diff from the last N commits.

    Args:
        commit_count: Number of commits to include in diff
        max_chars: Maximum characters to return
        cwd: Working directory for git commands (project repo path)

    Returns:
        Combined diff string, or None if not available
    """
    result = run_git_command(["git", "diff", f"HEAD~{commit_count}..HEAD"], cwd=cwd, timeout=30)
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return None

    diff: str = result.stdout
    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n\n... [diff truncated] ..."

    return diff


def get_commits_since(
    since_sha: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    cwd: str | Path | None = None,
) -> str | None:
    """Get diff from a specific commit SHA to HEAD.

    Args:
        since_sha: Starting commit SHA
        max_chars: Maximum characters to return
        cwd: Working directory for git commands (project repo path)

    Returns:
        Diff string, or None if not available
    """
    result = run_git_command(["git", "diff", f"{since_sha}..HEAD"], cwd=cwd, timeout=30)
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return None

    diff: str = result.stdout
    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n\n... [diff truncated] ..."

    return diff


def extract_file_patterns_from_text(text: str) -> list[str]:
    """Extract file paths and patterns from text (criteria, description, title).

    Looks for:
    - Explicit file paths (src/foo/bar.py, tests/test_foo.py)
    - Module references (gobby.tasks.validation -> src/gobby/tasks/validation.py)
    - Test patterns (test_validation -> tests/**/test_validation*.py)

    Args:
        text: Text to search for file patterns

    Returns:
        List of file path patterns (may include globs)
    """
    patterns: set[str] = set()

    # Match explicit file paths like src/foo/bar.py or ./tests/test_x.py
    file_path_re = re.compile(r"[./]?[\w\-]+(?:/[\w\-]+)*\.\w+")
    for match in file_path_re.findall(text):
        # Skip URLs and common false positives
        if not match.startswith("http") and not match.startswith("www."):
            patterns.add(match.lstrip("./"))

    # Match module references like gobby.tasks.validation
    module_re = re.compile(r"\b(gobby(?:\.\w+)+)\b")
    for match in module_re.findall(text):
        # Convert module path to file path
        file_path = "src/" + match.replace(".", "/") + ".py"
        patterns.add(file_path)

    # Extract test file hints from test_ prefixed words
    test_re = re.compile(r"\btest_(\w+)\b")
    for match in test_re.findall(text):
        patterns.add(f"tests/**/test_{match}*.py")

    # Extract class/function names and look for their definitions
    class_re = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:Manager|Validator|Plugin|Handler|Service))\b")
    for match in class_re.findall(text):
        # These could be in any .py file, add as grep pattern hint
        patterns.add(
            f"**/{''.join(c if c.islower() else '_' + c.lower() for c in match).lstrip('_')}*.py"
        )

    return list(patterns)


def find_matching_files(
    patterns: list[str],
    base_dir: str | Path = ".",
    max_files: int = 10,
) -> list[Path]:
    """Find files matching the given patterns.

    Args:
        patterns: List of file path patterns (may include globs)
        base_dir: Base directory to search from
        max_files: Maximum number of files to return

    Returns:
        List of Path objects for matching files
    """
    base = Path(base_dir)
    found: list[Path] = []

    for pattern in patterns:
        if len(found) >= max_files:
            break

        # Handle glob patterns
        if "*" in pattern:
            try:
                matches = list(base.glob(pattern))
                for match in matches[: max_files - len(found)]:
                    if match.is_file() and match not in found:
                        found.append(match)
            except Exception as e:
                logger.debug(f"Failed to glob pattern {pattern}: {e}")
        else:
            # Direct file path
            path = base / pattern
            if path.is_file() and path not in found:
                found.append(path)

    return found


def read_files_content(
    files: list[Path],
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Read content from multiple files.

    Args:
        files: List of file paths to read
        max_chars: Maximum total characters to return

    Returns:
        Concatenated file contents with headers
    """
    content_parts: list[str] = []
    total_chars = 0

    for file_path in files:
        if total_chars >= max_chars:
            content_parts.append("\n... [additional files truncated] ...")
            break

        try:
            content = file_path.read_text(encoding="utf-8")
            remaining = max_chars - total_chars

            if len(content) > remaining:
                content = content[:remaining] + "\n... [file truncated] ..."

            content_parts.append(f"=== {file_path} ===\n{content}\n")
            total_chars += len(content)

        except Exception as e:
            logger.debug(f"Failed to read {file_path}: {e}")
            content_parts.append(f"=== {file_path} ===\n(Error reading file: {e})\n")

    return "\n".join(content_parts)


def get_validation_context_smart(
    task_title: str,
    validation_criteria: str | None = None,
    task_description: str | None = None,
    commit_window: int = DEFAULT_COMMIT_WINDOW,
    max_chars: int = DEFAULT_MAX_CHARS,
    cwd: str | Path | None = None,
) -> str | None:
    """Gather validation context using multiple strategies.

    Multi-strategy context gathering:
    1. Current uncommitted changes (staged + unstaged)
    2. Multi-commit window (last N commits, configurable)
    3. File-based analysis (read files mentioned in criteria)

    TODO: Add strategy 4 - codebase grep for test files related to the task.
          Implementation location: after Strategy 3 below.

    Args:
        task_title: Task title for context
        validation_criteria: Validation criteria text
        task_description: Task description text
        commit_window: Number of commits to look back
        max_chars: Maximum characters to return
        cwd: Working directory for git commands (project repo path)

    Returns:
        Validation context string, or None if nothing found
    """
    context_parts: list[str] = []
    remaining_chars = max_chars

    # Strategy 1: Current uncommitted changes
    staged = run_git_command(["git", "diff", "--cached"], cwd=cwd)
    if staged and staged.stdout.strip():
        content = staged.stdout[: remaining_chars // 2]
        context_parts.append(f"=== STAGED CHANGES ===\n{content}")
        remaining_chars -= len(content)

    unstaged = run_git_command(["git", "diff"], cwd=cwd)
    if unstaged and unstaged.stdout.strip():
        content = unstaged.stdout[: remaining_chars // 2]
        context_parts.append(f"=== UNSTAGED CHANGES ===\n{content}")
        remaining_chars -= len(content)

    # Strategy 2: Multi-commit window
    if remaining_chars > 5000:  # Only if we have room
        multi_diff = get_multi_commit_diff(commit_window, remaining_chars // 2, cwd=cwd)
        if multi_diff:
            # Get commit list for context
            commits = get_recent_commits(commit_window, cwd=cwd)
            commit_summary = "\n".join(
                f"  - {c['sha'][:8]}: {c['subject'][:60]}" for c in commits[:5]
            )

            context_parts.append(
                f"=== RECENT COMMITS (last {commit_window}) ===\n"
                f"{commit_summary}\n\n"
                f"=== COMBINED DIFF ===\n{multi_diff}"
            )
            remaining_chars -= len(multi_diff) + len(commit_summary)

    # Strategy 3: File-based analysis
    if remaining_chars > 2000:
        # Extract file patterns from task info
        search_text = f"{task_title} {validation_criteria or ''} {task_description or ''}"
        patterns = extract_file_patterns_from_text(search_text)

        if patterns:
            files = find_matching_files(patterns, base_dir=cwd or ".", max_files=5)
            if files:
                file_content = read_files_content(files, remaining_chars)
                context_parts.append(f"=== RELEVANT FILES ===\n{file_content}")

    if not context_parts:
        return None

    combined = "\n\n".join(context_parts)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n\n... [context truncated] ..."

    return combined


def get_git_diff(
    max_chars: int = 50000,
    fallback_to_last_commit: bool = True,
    cwd: str | Path | None = None,
) -> str | None:
    """Get changes from git for validation.

    First checks for uncommitted changes (staged + unstaged).
    If none found and fallback_to_last_commit is True, returns the last commit's diff.

    Args:
        max_chars: Maximum characters to return (truncates if larger)
        fallback_to_last_commit: If True, fall back to last commit diff when no uncommitted changes
        cwd: Working directory for git commands (project repo path)

    Returns:
        Combined diff string, or None if not in git repo or no changes
    """
    unstaged = run_git_command(["git", "diff"], cwd=cwd)
    staged = run_git_command(["git", "diff", "--cached"], cwd=cwd)

    # Check if both commands failed (not in git repo or git error)
    unstaged_failed = unstaged is None or unstaged.returncode != 0
    staged_failed = staged is None or staged.returncode != 0
    if unstaged_failed and staged_failed:
        return None

    diff_parts = []
    if staged and staged.stdout.strip():
        diff_parts.append("=== STAGED CHANGES ===\n" + staged.stdout)
    if unstaged and unstaged.stdout.strip():
        diff_parts.append("=== UNSTAGED CHANGES ===\n" + unstaged.stdout)

    # If no uncommitted changes, try last commit
    if not diff_parts and fallback_to_last_commit:
        last_commit_diff = get_last_commit_diff(max_chars, cwd=cwd)
        if last_commit_diff:
            return f"=== LAST COMMIT ===\n{last_commit_diff}"
        return None

    if not diff_parts:
        return None

    combined = "\n".join(diff_parts)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n\n... [diff truncated] ..."

    return combined


@dataclass
class ValidationResult:
    """Result of task validation."""

    status: Literal["valid", "invalid", "pending"]
    feedback: str | None = None


class TaskValidator:
    """Validates task completion using LLM."""

    def __init__(
        self,
        config: TaskValidationConfig,
        llm_service: LLMService,
        project_dir: Path | None = None,
        db: DatabaseProtocol | None = None,
    ):
        self.config = config
        self.llm_service = llm_service
        self._loader = PromptLoader(db=db)

    async def gather_validation_context(self, file_paths: list[str]) -> str:
        """
        Gather context for validation from files.

        Args:
            file_paths: List of absolute file paths to read.

        Returns:
            Concatenated file contents.
        """
        context: list[str] = []
        for path in file_paths:
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()
                    context.append(f"--- {path} ---\n{content}\n")
            except Exception as e:
                logger.warning(f"Failed to read file {path} for validation: {e}")
                context.append(f"--- {path} ---\n(Error reading file: {e})\n")
        return "\n".join(context)

    async def validate_task(
        self,
        task_id: str,
        title: str,
        description: str | None,
        changes_summary: str,
        validation_criteria: str | None = None,
        context_files: list[str] | None = None,
        category: str | None = None,
    ) -> ValidationResult:
        """
        Validate task completion.

        Args:
            task_id: Task ID
            title: Task title
            description: Task description (used as fallback if no validation_criteria)
            changes_summary: Summary of changes made (files, diffs, etc.)
            validation_criteria: Specific criteria to validate against (optional)
            context_files: List of files to read for context (optional)
            category: Task domain category (e.g., 'manual', 'code', 'test')

        Returns:
            ValidationResult with status and feedback
        """
        if not self.config.enabled:
            return ValidationResult(status="pending", feedback="Validation disabled")

        if not description and not validation_criteria:
            logger.warning(f"Cannot validate task {task_id}: missing description and criteria")
            return ValidationResult(
                status="pending", feedback="Missing task description and validation criteria"
            )

        logger.info(f"Validating task {task_id}: {title}")

        # Gather context if provided
        file_context = ""
        if context_files:
            file_context = await self.gather_validation_context(context_files)

        # Build prompt
        criteria_text = (
            f"Validation Criteria:\n{validation_criteria}"
            if validation_criteria
            else f"Task Description:\n{description}"
        )

        # Detect if changes_summary is a git diff
        is_git_diff = changes_summary.startswith("Git diff") or "@@" in changes_summary

        if is_git_diff:
            changes_section = (
                "Code Changes (git diff):\n"
                "Analyze these ACTUAL code changes to verify the implementation.\n\n"
                f"{changes_summary}\n\n"
            )
        else:
            changes_section = f"Changes Summary:\n{changes_summary}\n\n"

        # Build test strategy section if provided
        category_section = ""
        if category:
            category_section = f"Test Strategy: {category}\n"
            if category.lower() == "manual":
                category_section += (
                    "NOTE: This task uses MANUAL testing. Do NOT require automated test files. "
                    "Validation should focus on whether the implementation is correct, "
                    "not whether automated tests exist.\n\n"
                )
            else:
                category_section += "\n"

        # Build prompt using PromptLoader
        prompt_path = self.config.prompt_path or "validation/validate"
        template_context = {
            "title": title,
            "category_section": category_section,
            "criteria_text": criteria_text,
            "changes_section": changes_section,
            "file_context": file_context[:50000] if file_context else "",
        }
        prompt = self._loader.render(prompt_path, template_context)

        try:
            provider = self.llm_service.get_provider(self.config.provider)
            response_content = await provider.generate_text(
                prompt=prompt,
                system_prompt=self.config.system_prompt,
                model=self.config.model,
            )

            if not response_content or not response_content.strip():
                logger.warning(f"Empty LLM response for task {task_id} validation")
                return ValidationResult(
                    status="pending", feedback="Validation failed: Empty response from LLM"
                )

            logger.debug(f"Validation LLM response for {task_id}: {response_content[:200]}...")

            # Extract JSON using shared utility
            result_data = extract_json_object(response_content)
            if result_data is None:
                logger.warning(f"Failed to parse JSON from validation response for {task_id}")
                return ValidationResult(
                    status="pending", feedback="Validation failed: Could not parse response"
                )

            return ValidationResult(
                status=result_data.get("status", "pending"), feedback=result_data.get("feedback")
            )

        except Exception as e:
            logger.error(f"Failed to validate task {task_id}: {e}")
            return ValidationResult(status="pending", feedback=f"Validation failed: {str(e)}")
