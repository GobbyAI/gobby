"""Commit linking and diff functionality for Task System V2.

Provides utilities for linking commits to tasks and computing diffs.
"""

import logging
import os
import re
import subprocess  # nosec B404 # internal git commands
import tempfile
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
from gobby.utils.git import git_subprocess_env, run_git_command

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager, Task

logger = logging.getLogger(__name__)


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


# `git hash-object -t tree /dev/null`: the base for a root commit's net patch.
_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_NET_PATCH_GIT_TIMEOUT_SECONDS = 30


def collect_commit_diff_text(
    commit_shas: list[str],
    *,
    cwd: str | Path,
) -> str:
    """Return the net patch of a prospective close commit set.

    The criteria review must judge the code as it stands after every linked
    commit, so the commits are replayed onto the first one's parent in a
    temporary index and diffed once: a later commit that rewrites an earlier
    one leaves no superseded hunk behind for the reviewer to mistake for the
    current code. A set that cannot be replayed (a linked commit that does not
    apply onto the others' history) falls back to the raw per-commit stream.
    """
    if not commit_shas:
        return ""
    net = _net_commit_patch(commit_shas, cwd=cwd)
    if net is not None:
        return net
    result = run_git_command(
        [
            "git",
            "show",
            "--format=",
            "--find-renames",
            "--find-copies",
            "--binary",
            *commit_shas,
        ],
        cwd=cwd,
        timeout=30,
    )
    if result is None:
        raise RuntimeError("git show failed while assembling the close criteria-review diff")
    return result


def _git_bytes(
    args: list[str],
    *,
    cwd: str | Path,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
) -> bytes | None:
    """Run one git command; stdout bytes on success, None on any failure."""
    base_env = git_subprocess_env() or os.environ
    try:
        completed = subprocess.run(  # nosec B603 # internal git command
            ["git", *args],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            timeout=_NET_PATCH_GIT_TIMEOUT_SECONDS,
            check=False,
            env={**base_env, **(env or {})},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("git %s failed while assembling the net close patch: %s", args[0], exc)
        return None
    if completed.returncode != 0:
        logger.debug(
            "git %s failed while assembling the net close patch: %s",
            args[0],
            completed.stderr.decode("utf-8", errors="replace").strip(),
        )
        return None
    return completed.stdout


def _ancestry_order(commit_shas: list[str], *, cwd: str | Path) -> list[str] | None:
    """Canonicalize the linked commits and order them oldest-first by topology.

    Commit timestamps cannot order commits made within one second, so the walk
    is topological, bounded below by the set's common ancestor.
    """
    resolved = _git_bytes(["rev-parse", *commit_shas], cwd=cwd)
    if not resolved:
        return None
    wanted = list(dict.fromkeys(resolved.decode("ascii", errors="replace").split()))
    common = _git_bytes(["merge-base", "--octopus", *wanted], cwd=cwd)
    if not common:
        return None
    floor = common.decode("ascii", errors="replace").strip()
    bounds: list[str] = []
    if _git_bytes(["rev-parse", "--verify", "--quiet", f"{floor}^"], cwd=cwd):
        bounds.append(f"^{floor}^")
    listed = _git_bytes(["rev-list", "--topo-order", "--reverse", *wanted, *bounds], cwd=cwd)
    if not listed:
        return None
    members = set(wanted)
    ordered = [sha for sha in listed.decode("ascii", errors="replace").split() if sha in members]
    return ordered if len(ordered) == len(members) else None


def _net_commit_patch(commit_shas: list[str], *, cwd: str | Path) -> str | None:
    """Replay the commits onto a temporary index and diff it against their base."""
    ordered = _ancestry_order(commit_shas, cwd=cwd)
    if not ordered:
        return None
    parent = _git_bytes(["rev-parse", "--verify", "--quiet", f"{ordered[0]}^"], cwd=cwd)
    base = parent.decode("ascii", errors="replace").strip() if parent else _EMPTY_TREE_SHA
    with tempfile.TemporaryDirectory(prefix="gobby-close-index-") as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        if _git_bytes(["read-tree", base], cwd=cwd, env=env) is None:
            return None
        for sha in ordered:
            patch = _git_bytes(
                ["show", "--format=", "--find-renames", "--find-copies", "--binary", sha],
                cwd=cwd,
            )
            if patch is None:
                return None
            if not patch.strip():
                continue
            applied = _git_bytes(
                ["apply", "--cached", "--binary", "--whitespace=nowarn", "-"],
                cwd=cwd,
                env=env,
                stdin=patch,
            )
            if applied is None:
                return None
        net = _git_bytes(
            ["diff", "--cached", "--find-renames", "--find-copies", "--binary", base],
            cwd=cwd,
            env=env,
        )
    if net is None:
        return None
    return net.decode("utf-8", errors="replace").strip()


# Doc file extensions that don't need LLM validation
DOC_EXTENSIONS = {".md", ".txt", ".rst", ".adoc", ".markdown"}


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


@dataclass(frozen=True)
class _TaggedCommit:
    sha: str
    task_refs: tuple[str, ...]


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
) -> tuple[set[str], "Task"] | None:
    """Return commit-message refs accepted for a task filter and its task."""
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
    return refs, task


def _task_tagged_git_history(
    task_manager: "LocalTaskManager",
    *,
    task_id: str | None,
    since: str | None,
    cwd: str | Path | None,
    project_name: str | None,
) -> list[_TaggedCommit]:
    """Return parsed task refs from the requested git history."""
    working_dir = Path(cwd) if cwd else Path.cwd()
    resolved_project_name = project_name or get_current_project_name()
    git_cmd = ["git", "log", "--reverse", "--pretty=format:%h|%s"]
    if task_id:
        branch = _resolve_branch_for_task(task_manager, task_id)
        if branch:
            git_cmd.append(branch)
    if since:
        git_cmd.append(f"--since={since}")

    log_output = run_git_command(git_cmd, cwd=working_dir)
    if not log_output:
        return []

    commits: list[_TaggedCommit] = []
    for line in log_output.strip().split("\n"):
        if not line or "|" not in line:
            continue
        commit_sha, message = line.split("|", 1)
        task_refs = tuple(extract_task_ids_from_message(message, resolved_project_name))
        if task_refs:
            commits.append(_TaggedCommit(sha=commit_sha, task_refs=task_refs))
    return commits


def resolve_task_tagged_commits(
    task_manager: "LocalTaskManager",
    *,
    task_id: str,
    since: str | None = None,
    cwd: str | Path | None = None,
    project_name: str | None = None,
    project_id: str | None = None,
) -> list[str]:
    """Resolve task-tagged commits without mutating task state."""
    task_filter = _resolve_task_filter(task_manager, task_id, project_id)
    if task_filter is None:
        return []
    accepted_refs, _task = task_filter
    history = _task_tagged_git_history(
        task_manager,
        task_id=task_id,
        since=since,
        cwd=cwd,
        project_name=project_name,
    )
    return [
        commit.sha
        for commit in history
        if any(task_ref in accepted_refs for task_ref in commit.task_refs)
    ]


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
    if task_id:
        task_filter = _resolve_task_filter(task_manager, task_id, project_id)
        if task_filter is None:
            return AutoLinkResult()
        accepted_refs, task = task_filter
        result = AutoLinkResult()
        seq_num = getattr(task, "seq_num", None)
        task_ref = f"#{seq_num}" if isinstance(seq_num, int) and seq_num > 0 else task_id
        existing_commits = list(task.commits or [])
        history = _task_tagged_git_history(
            task_manager,
            task_id=task_id,
            since=since,
            cwd=cwd,
            project_name=project_name,
        )
        for commit in history:
            for found_ref in commit.task_refs:
                if found_ref in accepted_refs:
                    continue
                try:
                    resolved_ref = found_ref
                    if project_id and (found_ref.startswith("#") or found_ref.isdigit()):
                        resolved_ref = task_manager.resolve_task_reference(found_ref, project_id)
                    task_manager.get_task(resolved_ref)
                except (TaskNotFoundError, ValueError):
                    result.skipped += 1
                    result.skipped_refs.setdefault(found_ref, []).append(commit.sha)
            if not any(found_ref in accepted_refs for found_ref in commit.task_refs):
                continue
            commit_sha = commit.sha
            if commit_sha in existing_commits:
                result.skipped += 1
                continue
            try:
                task_manager.link_commit(task.id, commit_sha, cwd=cwd)
            except ValueError as error:
                logger.debug("Skipping commit %s for task %s: %s", commit_sha, task_ref, error)
                result.skipped += 1
                continue
            result.linked_tasks.setdefault(task_ref, []).append(commit_sha)
            result.total_linked += 1
            existing_commits.append(commit_sha)
        return result

    result = AutoLinkResult()
    history = _task_tagged_git_history(
        task_manager,
        task_id=None,
        since=since,
        cwd=cwd,
        project_name=project_name,
    )
    for commit in history:
        commit_sha = commit.sha
        # Try to link each found task
        for tid in commit.task_refs:
            try:
                # Resolve #N format to UUID for database operations
                resolved_tid = tid
                if project_id and (tid.startswith("#") or tid.isdigit()):
                    resolved_tid = task_manager.resolve_task_reference(tid, project_id)

                task = task_manager.get_task(resolved_tid)
            except (TaskNotFoundError, ValueError):
                logger.debug("Skipping commit %s: task %s not found", commit_sha, tid)
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
                logger.debug("Skipping commit %s for task %s: %s", commit_sha, tid, error)
                result.skipped += 1
                continue

            # Track in result using original #N format for readability
            if tid not in result.linked_tasks:
                result.linked_tasks[tid] = []
            result.linked_tasks[tid].append(commit_sha)
            result.total_linked += 1

            logger.debug("Auto-linked commit %s to task %s", commit_sha, tid)

    return result
