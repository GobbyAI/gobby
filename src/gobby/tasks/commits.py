"""Commit linking and diff functionality for Task System V2.

Provides utilities for linking commits to tasks and computing diffs.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.utils.git import run_git_command

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)


@dataclass
class TaskDiffResult:
    """Result of computing a task's diff.

    Attributes:
        diff: Combined diff content from all linked commits
        commits: List of commit SHAs included in the diff
        has_uncommitted_changes: Whether uncommitted changes were included
        file_count: Number of files modified in the diff
    """

    diff: str
    commits: list[str] = field(default_factory=list)
    has_uncommitted_changes: bool = False
    file_count: int = 0


def get_task_diff(
    task_id: str,
    task_manager: "LocalTaskManager",
    include_uncommitted: bool = False,
    cwd: str | Path | None = None,
) -> TaskDiffResult:
    """Get the combined diff for all commits linked to a task.

    Args:
        task_id: The task ID to get diff for.
        task_manager: LocalTaskManager instance to fetch task data.
        include_uncommitted: If True, include uncommitted changes in diff.
        cwd: Working directory for git commands. Defaults to current directory.

    Returns:
        TaskDiffResult with combined diff and metadata.

    Raises:
        ValueError: If task not found.
    """
    # Get the task (raises ValueError if not found)
    task = task_manager.get_task(task_id)

    # Handle no commits
    commits = task.commits or []
    if not commits and not include_uncommitted:
        return TaskDiffResult(diff="", commits=[], has_uncommitted_changes=False)

    working_dir = Path(cwd) if cwd else Path.cwd()
    diff_parts = []
    has_uncommitted = False

    # Get diff for each linked commit
    if commits:
        # For multiple commits, we get the combined diff
        # git diff <first_commit>^..<last_commit> shows all changes
        if len(commits) == 1:
            # Single commit: show its changes
            result = run_git_command(
                ["git", "show", "--format=", commits[0]],
                cwd=working_dir,
            )
            if result:
                diff_parts.append(result)
        else:
            # Multiple commits: get combined diff
            # Commits are stored in chronological order (oldest at index 0, newest at index -1)
            # git diff oldest^..newest shows all changes in the range
            result = run_git_command(
                ["git", "diff", f"{commits[0]}^..{commits[-1]}"],
                cwd=working_dir,
            )
            if result:
                diff_parts.append(result)

    # Include uncommitted changes if requested
    if include_uncommitted:
        uncommitted = run_git_command(
            ["git", "diff", "HEAD"],
            cwd=working_dir,
        )
        if uncommitted:
            diff_parts.append(uncommitted)
            has_uncommitted = True

    # Combine all diff parts
    combined_diff = "\n".join(diff_parts)

    # Count files in the diff
    file_count = len(re.findall(r"^diff --git", combined_diff, re.MULTILINE))

    return TaskDiffResult(
        diff=combined_diff,
        commits=commits,
        has_uncommitted_changes=has_uncommitted,
        file_count=file_count,
    )


# Doc file extensions that don't need LLM validation
DOC_EXTENSIONS = {".md", ".txt", ".rst", ".adoc", ".markdown"}


def is_doc_only_diff(diff: str) -> bool:
    """Check if a diff only affects documentation files.

    Args:
        diff: Git diff string.

    Returns:
        True if all modified files are documentation files.
    """
    if not diff:
        return False

    # Find all file paths in the diff
    file_pattern = r"^diff --git a/(.+?) b/"
    matches = re.findall(file_pattern, diff, re.MULTILINE)

    if not matches:
        return False

    # Check if all files are doc files
    for file_path in matches:
        ext = Path(file_path).suffix.lower()
        if ext not in DOC_EXTENSIONS:
            return False

    return True


def summarize_diff_for_validation(
    diff: str,
    max_chars: int = 30000,
    max_hunk_lines: int = 50,
    priority_files: list[str] | None = None,
) -> str:
    """Summarize a diff for LLM validation, ensuring all files are visible.

    For large diffs, this:
    1. Always shows the complete file list with stats
    2. Truncates individual hunks to avoid overwhelming the LLM
    3. Prioritizes showing file names over full content
    4. When priority_files provided, shows those files first with more space

    Args:
        diff: Full git diff string.
        max_chars: Maximum characters to return.
        max_hunk_lines: Maximum lines per hunk before truncation.
        priority_files: Optional list of file paths to prioritize.
            These files appear first and get 60% of the space allocation.

    Returns:
        Summarized diff string that fits within max_chars.
    """
    if not diff or len(diff) <= max_chars:
        return diff

    # Parse the diff into files
    file_diffs = re.split(r"(?=^diff --git)", diff, flags=re.MULTILINE)
    file_diffs = [f for f in file_diffs if f.strip()]

    if not file_diffs:
        return diff[:max_chars] + "\n\n... [diff truncated] ..."

    # First, collect file stats
    file_stats: list[dict[str, str | int]] = []
    for file_diff in file_diffs:
        # Extract file name
        name_match = re.match(r"diff --git a/(.+?) b/", file_diff)
        if name_match:
            file_name = name_match.group(1)
        else:
            file_name = "(unknown)"

        # Count additions/deletions
        additions = len(re.findall(r"^\+[^+]", file_diff, re.MULTILINE))
        deletions = len(re.findall(r"^-[^-]", file_diff, re.MULTILINE))

        file_stats.append(
            {
                "name": file_name,
                "additions": additions,
                "deletions": deletions,
                "diff": file_diff,
            }
        )

    # Separate into priority and non-priority groups if priority_files provided
    priority_stats: list[dict[str, str | int]] = []
    non_priority_stats: list[dict[str, str | int]] = []

    if priority_files:
        priority_set = set(priority_files)
        for f in file_stats:
            if str(f["name"]) in priority_set:
                priority_stats.append(f)
            else:
                non_priority_stats.append(f)
    else:
        # No priority files - all are non-priority (original behavior)
        non_priority_stats = file_stats

    # Build summary header
    total_additions = sum(int(f["additions"]) for f in file_stats)
    total_deletions = sum(int(f["deletions"]) for f in file_stats)

    summary_parts: list[str] = [
        f"## Diff Summary ({len(file_stats)} files, +{total_additions}/-{total_deletions})\n",
        "### Files Changed:\n",
    ]

    # Show priority files first in the summary
    if priority_stats:
        summary_parts.append("#### Priority Files:\n")
        for f in priority_stats:
            summary_parts.append(f"- {f['name']} (+{f['additions']}/-{f['deletions']})\n")
        if non_priority_stats:
            summary_parts.append("\n#### Other Files:\n")

    for f in non_priority_stats:
        summary_parts.append(f"- {f['name']} (+{f['additions']}/-{f['deletions']})\n")

    summary_parts.append("\n### File Details:\n\n")

    # Calculate remaining space for file contents
    header_size = sum(len(p) for p in summary_parts)
    remaining_chars = max_chars - header_size - 100  # Buffer for truncation message

    # Allocate space: 60% to priority files, 40% to non-priority (if priority_files provided)
    if priority_files and priority_stats:
        priority_space = int(remaining_chars * 0.6)
        non_priority_space = remaining_chars - priority_space

        chars_per_priority = priority_space // len(priority_stats) if priority_stats else 0
        chars_per_non_priority = (
            non_priority_space // len(non_priority_stats) if non_priority_stats else 0
        )
    else:
        # Original behavior: equal distribution
        chars_per_priority = 0
        chars_per_non_priority = (
            remaining_chars // len(file_stats) if file_stats else remaining_chars
        )

    def truncate_file_content(file_content: str, max_file_chars: int) -> str:
        """Truncate a file diff to fit within max_file_chars."""
        if len(file_content) <= max_file_chars:
            return file_content

        # Truncate this file's diff but keep the header
        header_end = file_content.find("@@")
        if header_end > 0:
            header = file_content[:header_end]
            hunks = file_content[header_end:]
            # Keep first part of hunks
            truncated_hunks = hunks[: max_file_chars - len(header) - 50]
            return header + truncated_hunks + "\n... [file diff truncated] ...\n"
        else:
            return file_content[:max_file_chars] + "\n... [file diff truncated] ...\n"

    # Add priority files first
    for f in priority_stats:
        file_content = truncate_file_content(str(f["diff"]), chars_per_priority)
        summary_parts.append(file_content)

    # Add non-priority files
    for f in non_priority_stats:
        file_content = truncate_file_content(str(f["diff"]), chars_per_non_priority)
        summary_parts.append(file_content)

    result = "".join(summary_parts)

    # Final safety check
    if len(result) > max_chars:
        result = result[:max_chars] + "\n\n... [diff truncated] ..."

    return result


def _build_file_patterns(
    file_extensions: list[str] | None = None,
    path_prefixes: list[str] | None = None,
) -> list[str]:
    """Build regex patterns for file path extraction.

    Args:
        file_extensions: List of file extensions to match (e.g., [".py", ".ts"]).
            If None, uses a basic default set.
        path_prefixes: List of path prefixes to match (e.g., ["src/", "tests/"]).
            If None, uses a basic default set.

    Returns:
        List of regex patterns for file path matching.
    """
    # Build extension pattern from config
    if file_extensions:
        # Strip leading dots and escape for regex
        exts = [ext.lstrip(".") for ext in file_extensions]
        ext_pattern = "|".join(re.escape(ext) for ext in exts)
    else:
        ext_pattern = "py|ts|js|json|yaml|yml|toml|md|go|rs|cfg|ini|sh"

    # Build prefix pattern from config
    if path_prefixes:
        # Strip trailing slashes for regex alternation
        prefixes = [p.rstrip("/") for p in path_prefixes]
        prefix_pattern = "|".join(re.escape(p) for p in prefixes)
    else:
        prefix_pattern = "src|tests?|lib|config|scripts?|docs?|bin|pkg|internal|cmd"

    return [
        # Backtick-quoted paths: `path/to/file.py`
        r"`([^`]+/[^`]+)`",
        r"`([^`]+\.[a-zA-Z0-9]+)`",
        # Paths with directory separators and extensions
        r"(?<![a-zA-Z0-9_])([a-zA-Z0-9_./-]+/[a-zA-Z0-9_.-]+\.[a-zA-Z0-9]+)",
        # Paths starting with common prefixes (using config)
        rf"(?<![a-zA-Z0-9_])((?:{prefix_pattern})/[a-zA-Z0-9_./+-]+)",
        # Absolute paths
        r"(/[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)+)",
        # Relative paths with ./
        r"(\./[a-zA-Z0-9_./+-]+)",
        # Standalone filenames with common extensions (using config)
        rf"(?<![a-zA-Z0-9_/])([a-zA-Z0-9_-]+\.(?:{ext_pattern}))\b",
    ]


# Default known files (used when no config provided)
_DEFAULT_KNOWN_FILES = {
    "Makefile",
    "Dockerfile",
    "Jenkinsfile",
    "Vagrantfile",
    "Rakefile",
    "Gemfile",
}


def extract_mentioned_files(
    task: dict[str, Any],
    file_extensions: list[str] | None = None,
    known_files: list[str] | None = None,
    path_prefixes: list[str] | None = None,
) -> list[str]:
    """Extract file paths mentioned in task title, description, and validation_criteria.

    Searches for file path patterns in the task's text fields and returns
    a deduplicated list of file paths. Useful for prioritizing relevant files
    in validation context.

    Args:
        task: Task dictionary with title, description, and optionally validation_criteria.
        file_extensions: List of file extensions to recognize (from config).
            If None, uses basic defaults.
        known_files: List of known filenames without extensions (from config).
            If None, uses basic defaults.
        path_prefixes: List of common path prefixes (from config).
            If None, uses basic defaults.

    Returns:
        List of unique file paths mentioned in the task.
    """
    # Combine text from all relevant fields
    text_parts = []
    if task.get("title"):
        text_parts.append(task["title"])
    if task.get("description"):
        text_parts.append(task["description"])
    if task.get("validation_criteria"):
        text_parts.append(task["validation_criteria"])

    if not text_parts:
        return []

    combined_text = "\n".join(text_parts)
    found_paths: set[str] = set()

    # Build patterns based on config
    patterns = _build_file_patterns(file_extensions, path_prefixes)

    # Apply each pattern
    for pattern in patterns:
        matches = re.findall(pattern, combined_text)
        for match in matches:
            # Clean up the match
            path = match.strip()
            # Skip if it looks like a URL
            if path.startswith("http://") or path.startswith("https://"):
                continue
            # Skip if too short or doesn't look like a path
            if len(path) < 3:
                continue
            found_paths.add(path)

    # Check for known filenames without extensions
    files_to_check = set(known_files) if known_files else _DEFAULT_KNOWN_FILES
    for filename in files_to_check:
        if filename in combined_text:
            # Only add if it appears as a word boundary (escape special chars in filename)
            escaped_filename = re.escape(filename)
            if re.search(rf"(?<![a-zA-Z0-9_/]){escaped_filename}(?![a-zA-Z0-9_])", combined_text):
                found_paths.add(filename)

    return list(found_paths)


def extract_mentioned_symbols(task: dict[str, Any]) -> list[str]:
    """Extract function/class names mentioned in task description.

    Searches for symbol patterns in backticks and extracts function/class names.
    Useful for providing enhanced context to validators.

    Args:
        task: Task dictionary with title, description, and optionally validation_criteria.

    Returns:
        List of unique symbol names mentioned in the task.
    """
    # Combine text from all relevant fields
    text_parts = []
    if task.get("title"):
        text_parts.append(task["title"])
    if task.get("description"):
        text_parts.append(task["description"])
    if task.get("validation_criteria"):
        text_parts.append(task["validation_criteria"])

    if not text_parts:
        return []

    combined_text = "\n".join(text_parts)
    found_symbols: set[str] = set()

    # Pattern to match backtick-quoted content
    backtick_pattern = r"`([^`]+)`"
    backtick_matches = re.findall(backtick_pattern, combined_text)

    for match in backtick_matches:
        match = match.strip()

        # Skip if it looks like a file path (contains / or has file extension pattern)
        if "/" in match:
            continue
        # Skip if it looks like a filename with common extensions
        if re.search(r"\.[a-zA-Z]{1,4}$", match) and "." in match:
            # But allow method calls like obj.method()
            if not re.search(r"^[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*(?:\(\))?$", match):
                continue

        # Extract the symbol name
        # Remove trailing () if present
        symbol = re.sub(r"\(\)$", "", match)

        # Handle Class.method pattern - extract the method name
        if "." in symbol:
            parts = symbol.split(".")
            # Add the method name (last part)
            method_name = parts[-1]
            if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", method_name):
                found_symbols.add(method_name)
            # Optionally also add the full reference
            if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*$", symbol):
                found_symbols.add(symbol)
        else:
            # Simple identifier (function name, class name, etc.)
            if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", symbol):
                found_symbols.add(symbol)

    return list(found_symbols)


# Task ID patterns to search for in commit messages
# Uses {project}-#N format to avoid GitHub auto-linking and match CLI display format
# Patterns capture both project name and task number for validation
TASK_ID_PATTERNS = [
    # [project-#N] - bracket format (primary)
    r"\[(\w+)-#(\d+)\]",
    # project-#N - standalone format (word boundary before, after digits)
    r"(?:^|\s)(\w+)-#(\d+)\b",
    # Implements/Fixes/Closes/Refs project-#N
    r"(?:implements|fixes|closes|refs)\s+(\w+)-#(\d+)",
]


def get_current_project_name() -> str | None:
    """Get current project name from context.

    Returns:
        Project name or None if not in a project.
    """
    from gobby.utils.project_context import get_project_context

    ctx = get_project_context()
    if ctx and ctx.get("name"):
        name: str = ctx["name"]
        return name
    return None


def extract_task_ids_from_message(
    message: str,
    project_name: str | None = None,
) -> list[str]:
    """Extract task IDs from a commit message.

    Supports patterns:
    - [project-#N] - bracket format (primary)
    - project-#N - standalone format
    - Implements/Fixes/Closes/Refs project-#N

    Args:
        message: Commit message to parse.
        project_name: Optional project name to filter matches. If provided,
            only returns task IDs from commits referencing this project.
            If None, returns all task IDs found regardless of project.

    Returns:
        List of unique task references found (e.g., ["#1", "#42"]).
    """
    task_ids = set()

    for pattern in TASK_ID_PATTERNS:
        matches = re.findall(pattern, message, re.IGNORECASE | re.MULTILINE)
        for match in matches:
            # match is a tuple: (project, task_number)
            found_project, task_num = match
            # Filter by project name if specified
            if project_name and found_project.lower() != project_name.lower():
                continue
            # Format as #N
            task_id = f"#{task_num}"
            task_ids.add(task_id)

    return list(task_ids)


@dataclass
class AutoLinkResult:
    """Result of auto-linking commits to tasks.

    Attributes:
        linked_tasks: Dict mapping task_id -> list of newly linked commit SHAs.
        total_linked: Total number of commits newly linked.
        skipped: Number of commits skipped (already linked or task not found).
    """

    linked_tasks: dict[str, list[str]] = field(default_factory=dict)
    total_linked: int = 0
    skipped: int = 0


def auto_link_commits(
    task_manager: "LocalTaskManager",
    task_id: str | None = None,
    since: str | None = None,
    cwd: str | Path | None = None,
    project_name: str | None = None,
    project_id: str | None = None,
) -> AutoLinkResult:
    """Auto-detect and link commits that mention task IDs.

    Searches commit messages for task ID patterns and links matching commits
    to the corresponding tasks.

    Args:
        task_manager: LocalTaskManager instance for task operations.
        task_id: Optional specific task ID to filter for (#N or UUID format).
        since: Optional git --since parameter (e.g., "1 week ago", "2024-01-01").
        cwd: Working directory for git commands.
        project_name: Optional project name to filter commits. If not provided,
            auto-detects from current project context.
        project_id: Project ID for resolving #N format task references.

    Returns:
        AutoLinkResult with details of linked and skipped commits.
    """
    working_dir = Path(cwd) if cwd else Path.cwd()

    # Get project name for filtering (auto-detect if not provided)
    if project_name is None:
        project_name = get_current_project_name()

    # Build git log command
    # Format: "sha|message" for easy parsing
    git_cmd = ["git", "log", "--pretty=format:%h|%s"]

    if since:
        git_cmd.append(f"--since={since}")

    # Get git log output
    log_output = run_git_command(git_cmd, cwd=working_dir)

    if not log_output:
        return AutoLinkResult()

    result = AutoLinkResult()

    # Parse each commit line
    for line in log_output.strip().split("\n"):
        if not line or "|" not in line:
            continue

        parts = line.split("|", 1)
        if len(parts) != 2:
            continue

        commit_sha, message = parts

        # Extract task IDs from message (filtered by project name)
        found_task_ids = extract_task_ids_from_message(message, project_name)

        if not found_task_ids:
            continue

        # Filter to specific task if requested
        if task_id:
            if task_id not in found_task_ids:
                continue
            found_task_ids = [task_id]

        # Try to link each found task
        for tid in found_task_ids:
            try:
                # Resolve #N format to UUID for database operations
                resolved_tid = tid
                if project_id and (tid.startswith("#") or tid.isdigit()):
                    resolved_tid = task_manager.resolve_task_reference(tid, project_id)

                task = task_manager.get_task(resolved_tid)

                # Check if already linked
                existing_commits = task.commits or []
                if commit_sha in existing_commits:
                    result.skipped += 1
                    continue

                # Link the commit using UUID
                task_manager.link_commit(task.id, commit_sha, cwd=cwd)

                # Track in result using original #N format for readability
                if tid not in result.linked_tasks:
                    result.linked_tasks[tid] = []
                result.linked_tasks[tid].append(commit_sha)
                result.total_linked += 1

                logger.debug(f"Auto-linked commit {commit_sha} to task {tid}")

            except ValueError:
                # Task doesn't exist, skip
                logger.debug(f"Skipping commit {commit_sha}: task {tid} not found")
                result.skipped += 1
                continue

    return result
