"""Commit linking and diff functionality for Task System V2.

Provides utilities for linking commits to tasks and computing diffs.
"""

import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.diff_paging import (
    MAX_COMMITS_LIMIT,
    MAX_LIMIT_BYTES,
    MAX_MANIFEST_LIMIT,
    DiffPage,
    DiffPagingError,
    decode_content,
    get_task_diff_page,
)
from gobby.utils.git import run_git_command

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)


def _strip_diff_path_prefix(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def collect_task_diff_text(
    task_id: str,
    task_manager: "LocalTaskManager",
    *,
    include_uncommitted: bool = False,
    cwd: str | Path | None = None,
) -> tuple[str, DiffPage]:
    """Collect every lossless page for transitional string consumers."""
    offset = 0
    snapshot_hash: str | None = None
    view_hash: str | None = None
    chunks: list[bytes] = []
    first_page: DiffPage | None = None
    while True:
        page = get_task_diff_page(
            task_id,
            task_manager,
            include_uncommitted=include_uncommitted,
            cwd=cwd,
            offset_bytes=offset,
            limit_bytes=MAX_LIMIT_BYTES,
            commits_limit=MAX_COMMITS_LIMIT if first_page is None else 0,
            manifest_limit=MAX_MANIFEST_LIMIT if first_page is None else 0,
            snapshot_hash=snapshot_hash,
            view_hash=view_hash,
        )
        if first_page is None:
            first_page = page
        chunks.append(decode_content(page["content"]))
        if page["complete"]:
            break
        if page["byte_end"] <= offset:
            raise DiffPagingError("paging_stalled", "diff paging made no byte progress")
        offset = page["byte_end"]
        snapshot_hash = page["snapshot_hash"]
        view_hash = page["view_hash"]
    assert first_page is not None
    return b"".join(chunks).decode("utf-8", errors="replace"), first_page


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

    files = _parse_diff_files(diff)
    if not files:
        return False

    for file in files:
        ext = Path(file.path).suffix.lower()
        if ext not in DOC_EXTENSIONS:
            return False

    return True


@dataclass(frozen=True)
class _DiffFile:
    path: str
    additions: int
    deletions: int
    diff: str


_FILE_DIFF_TRUNCATION_MARKER = "\n... [file diff truncated] ...\n"
_DIFF_TRUNCATION_MARKER = "\n... [diff truncated] ...\n"
_DIFF_TOO_LARGE_MESSAGE = (
    "## Diff Summary\n\n"
    "Diff too large to validate safely: the changed-file manifest does not fit "
    "inside the validation budget. Close-task validation should return pending "
    "instead of treating omitted files as missing.\n"
)


def _diff_path_from_header(header: str) -> str:
    try:
        parts = shlex.split(header.removeprefix("diff --git "))
    except ValueError:
        parts = []
    if len(parts) >= 2:
        old_path = _strip_diff_path_prefix(parts[0])
        new_path = _strip_diff_path_prefix(parts[1])
        return new_path if new_path != "/dev/null" else old_path

    match = re.match(r"diff --git a/(.+?) b/", header)
    return match.group(1) if match else "(unknown)"


def _parse_diff_files(diff: str) -> list[_DiffFile]:
    file_diffs = re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE)
    parsed: list[_DiffFile] = []
    for file_diff in file_diffs:
        if not file_diff.strip():
            continue
        lines = file_diff.splitlines()
        path = _diff_path_from_header(lines[0] if lines else "")
        additions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        parsed.append(
            _DiffFile(path=path, additions=additions, deletions=deletions, diff=file_diff)
        )
    return parsed


def changed_files_from_diff(diff: str | None) -> list[str]:
    """Return changed file paths from a git diff in diff order."""
    if not diff:
        return []
    return [file.path for file in _parse_diff_files(diff)]


def _priority_key(path: str, priority_files: list[str] | None) -> tuple[int, str]:
    if not priority_files:
        return (1, path)
    normalized = path.lstrip("./")
    for priority in priority_files:
        cleaned = priority.lstrip("./")
        if normalized == cleaned or normalized.endswith(f"/{cleaned}"):
            return (0, path)
        if "/" not in cleaned and Path(normalized).name == cleaned:
            return (0, path)
    return (1, path)


def _limit_hunk_lines(file_diff: str, max_hunk_lines: int) -> str:
    if max_hunk_lines <= 0:
        return file_diff

    kept: list[str] = []
    hunk_line_count = 0
    truncated = False
    in_hunk = False
    for line in file_diff.splitlines(keepends=True):
        if line.startswith("@@"):
            in_hunk = True
            hunk_line_count = 0
            kept.append(line)
            continue
        if in_hunk and line and line[0] in "+- ":
            hunk_line_count += 1
            if hunk_line_count > max_hunk_lines:
                truncated = True
                continue
        kept.append(line)

    limited = "".join(kept)
    if truncated:
        limited = limited.rstrip() + _FILE_DIFF_TRUNCATION_MARKER
    return limited


def summarize_diff_for_validation(
    diff: str | None,
    max_chars: int = 30000,
    max_hunk_lines: int = 50,
    priority_files: list[str] | None = None,
) -> str | None:
    """Render structured diff evidence for LLM validation.

    The changed-file manifest is complete and authoritative. Large raw details
    are excerpted with named omissions instead of anonymous truncation markers.
    """
    if diff is None:
        return None
    if not diff:
        return diff

    from gobby.tasks.validation_evidence import build_diff_validation_evidence

    return build_diff_validation_evidence(
        diff,
        max_chars=max_chars,
        max_hunk_lines=max_hunk_lines,
        priority_files=priority_files,
    ).text


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
        r"(?<![a-zA-Z0-9_.-])(/[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)+)",
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
    r"\[(\w+(?:[ -]\w+)*)-#(\d+)\]",
    # project-#N - standalone format (word boundary before, after digits)
    r"(?:^|\s)(\w+(?:[ -]\w+)*)-#(\d+)\b",
    # Implements/Fixes/Closes/Refs project-#N
    r"(?:implements|fixes|closes|refs)\s+(\w+(?:[ -]\w+)*)-#(\d+)",
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
        skipped_refs: Dict mapping unknown task refs -> commit SHAs that mentioned them.
    """

    linked_tasks: dict[str, list[str]] = field(default_factory=dict)
    total_linked: int = 0
    skipped: int = 0
    skipped_refs: dict[str, list[str]] = field(default_factory=dict)


def _resolve_branch_for_task(
    task_manager: "LocalTaskManager",
    task_id: str,
) -> str | None:
    """Resolve the worktree/clone branch name for a task.

    Walks up the parent chain to find the epic, then looks up the
    worktree or clone record to get the branch name.

    Returns the branch name, or None if no isolation is configured.
    """
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.worktrees import LocalWorktreeManager

    db = task_manager.db
    wt_mgr = LocalWorktreeManager(db)
    clone_mgr = LocalCloneManager(db)

    current_id: str | None = task_id
    visited: set[str] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)

        wt = wt_mgr.get_by_task(current_id)
        if wt and wt.branch_name:
            return wt.branch_name

        clone = clone_mgr.get_by_task(current_id)
        if clone and clone.branch_name:
            return clone.branch_name

        try:
            task = task_manager.get_task(current_id)
            current_id = task.parent_task_id if task else None
        except Exception:
            break

    return None


def _resolve_task_filter(
    task_manager: "LocalTaskManager",
    task_id: str,
    project_id: str | None,
) -> tuple[set[str], str] | None:
    """Return commit-message refs accepted for a task filter and its DB ID."""
    resolved_task_id = task_id
    if project_id and (task_id.startswith("#") or task_id.isdigit()):
        resolved_task_id = task_manager.resolve_task_reference(task_id, project_id)

    try:
        task = task_manager.get_task(resolved_task_id)
    except Exception:
        return None

    refs = {task_id, task.id}
    seq_num = getattr(task, "seq_num", None)
    if isinstance(seq_num, int):
        refs.add(f"#{seq_num}")
        refs.add(str(seq_num))
    return refs, task.id


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
    git_cmd = ["git", "log", "--reverse", "--pretty=format:%h|%s"]

    # When a task_id is provided, resolve the isolation branch so we
    # search the correct branch even when cwd is the main repo.
    if task_id:
        branch = _resolve_branch_for_task(task_manager, task_id)
        if branch:
            git_cmd.append(branch)

    if since:
        git_cmd.append(f"--since={since}")

    # Get git log output
    log_output = run_git_command(git_cmd, cwd=working_dir)

    if not log_output:
        return AutoLinkResult()

    result = AutoLinkResult()
    task_filter_refs: set[str] | None = None
    task_filter_resolved_id: str | None = None
    if task_id:
        task_filter = _resolve_task_filter(task_manager, task_id, project_id)
        if task_filter is not None:
            task_filter_refs, task_filter_resolved_id = task_filter

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
            accepted_refs = task_filter_refs or {task_id}
            matched_ref = next((tid for tid in found_task_ids if tid in accepted_refs), None)
            if matched_ref is None:
                continue
            found_task_ids = [matched_ref]

        # Try to link each found task
        for tid in found_task_ids:
            try:
                # Resolve #N format to UUID for database operations
                resolved_tid = (task_filter_resolved_id or tid) if task_id else tid
                if not task_id and project_id and (tid.startswith("#") or tid.isdigit()):
                    resolved_tid = task_manager.resolve_task_reference(tid, project_id)

                task = task_manager.get_task(resolved_tid)
            except (TaskNotFoundError, ValueError):
                logger.debug(f"Skipping commit {commit_sha}: task {tid} not found")
                result.skipped += 1
                result.skipped_refs.setdefault(tid, []).append(commit_sha)
                continue

            # Check if already linked
            existing_commits = task.commits or []
            if commit_sha in existing_commits:
                result.skipped += 1
                continue

            try:
                # Link the commit using UUID
                task_manager.link_commit(task.id, commit_sha, cwd=cwd)
            except ValueError as error:
                logger.debug(f"Skipping commit {commit_sha} for task {tid}: {error}")
                result.skipped += 1
                continue

            # Track in result using original #N format for readability
            if tid not in result.linked_tasks:
                result.linked_tasks[tid] = []
            result.linked_tasks[tid].append(commit_sha)
            result.total_linked += 1

            logger.debug(f"Auto-linked commit {commit_sha} to task {tid}")

    return result
