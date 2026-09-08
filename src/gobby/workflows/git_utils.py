"""Git utility functions for workflow actions.

Extracted from actions.py as part of strangler fig decomposition.
These are pure utility functions with no ActionContext dependency.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess  # nosec B404 - synchronous compatibility helpers are offline-only
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.utils.daemon_git import GitFailed, GitOk, GitResult, daemon_git, parse_porcelain_v1_z

if TYPE_CHECKING:
    from gobby.storage.session_tasks import SessionTaskManager
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

# ``git status --porcelain=v2 -z`` costs tens of milliseconds on a healthy repo,
# so this is headroom for a loaded machine rather than a working budget. It is
# deliberately well inside the shared blocking-effect budget: a caller parked
# here holds one of the workflow runtime's few blocking threads.
DEFAULT_GIT_STATUS_TIMEOUT_SECONDS = 5.0
GIT_STATUS_UNAVAILABLE_MARKER = "__gobby_git_status_unavailable__"


class GitStatusUnavailable(RuntimeError):
    """Git status could not be determined, so cleanliness is unknown."""


def _require_git_ok(result: GitResult, operation: str) -> GitOk:
    if isinstance(result, GitOk):
        return result
    detail = result.stderr.strip() or result.status
    raise GitStatusUnavailable(f"{operation} unavailable: {detail}")


async def get_git_status_async(project_path: str | None = None) -> str:
    """Get git status for a project directory.

    Args:
        project_path: Optional path to the project directory.

    Returns:
        Short git status output, or error message if not a git repo.
    """
    cwd = project_path or Path.cwd()
    result = _require_git_ok(await daemon_git.status(cwd, timeout=5.0), "Git status")
    entries = parse_porcelain_v1_z(result.stdout)
    return "\n".join(f"{entry.code} {entry.path}" for entry in entries) or "No changes"


def get_git_status(project_path: str | None = None) -> str:
    """Get status synchronously for offline CLI callers only."""
    try:
        result = subprocess.run(  # nosec B603 B607 - fixed offline Git argv
            ["git", "status", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_path,
        )
        return result.stdout.strip() or "No changes"
    except Exception:
        return "Not a git repository or git not available"


async def get_recent_git_commits_async(
    max_commits: int = 10,
    project_path: str | None = None,
) -> list[dict[str, str]]:
    """Get recent git commits with hash and message.

    Args:
        max_commits: Maximum number of commits to return
        project_path: Optional path to the project directory.

    Returns:
        List of dicts with 'hash' and 'message' keys
    """
    result = _require_git_ok(
        await daemon_git.run(
            ["log", f"-{max_commits}", "--format=%H|%s"],
            cwd=project_path or Path.cwd(),
            timeout=5.0,
        ),
        "Git log",
    )
    commits = []
    for line in result.stdout.strip().split("\n"):
        if "|" in line:
            hash_part, message = line.split("|", 1)
            commits.append({"hash": hash_part, "message": message})
    return commits


def get_recent_git_commits(
    max_commits: int = 10, project_path: str | None = None
) -> list[dict[str, str]]:
    """Get recent commits synchronously for offline CLI callers only."""
    try:
        result = subprocess.run(  # nosec B603 B607 - fixed offline Git argv
            ["git", "log", f"-{max_commits}", "--format=%H|%s"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_path,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    commits = []
    for line in result.stdout.strip().split("\n"):
        if "|" in line:
            hash_part, message = line.split("|", 1)
            commits.append({"hash": hash_part, "message": message})
    return commits


async def get_file_changes_async(
    project_path: str | None = None,
    paths: Sequence[str] | None = None,
) -> str:
    """Get detailed file changes from git.

    Args:
        project_path: Optional path to the project directory. When provided,
            git commands run in this directory instead of the current working directory.
        paths: Optional repository paths that bound the returned changes.

    Returns:
        Formatted string with modified/deleted and untracked files.
    """
    path_args = ["--", *paths] if paths else []
    cwd = project_path or Path.cwd()
    diff_raw, untracked_raw = await asyncio.gather(
        daemon_git.run(
            ["--literal-pathspecs", "diff", "HEAD", "--name-status", *path_args],
            cwd=cwd,
            timeout=5.0,
        ),
        daemon_git.run(
            [
                "--literal-pathspecs",
                "ls-files",
                "--others",
                "--exclude-standard",
                *path_args,
            ],
            cwd=cwd,
            timeout=5.0,
        ),
    )
    diff_result = _require_git_ok(diff_raw, "Git diff status")
    untracked_result = _require_git_ok(untracked_raw, "Git untracked status")

    changes = []
    if diff_result.stdout.strip():
        changes.extend(("Modified/Deleted:", diff_result.stdout.strip()))
    if untracked_result.stdout.strip():
        changes.extend(("\nUntracked:", untracked_result.stdout.strip()))
    return "\n".join(changes) if changes else "No changes"


def get_file_changes(project_path: str | None = None, paths: Sequence[str] | None = None) -> str:
    """Get file changes synchronously for offline CLI callers only."""
    try:
        path_args = ["--", *paths] if paths else []
        diff_result = subprocess.run(  # nosec B603 B607 - fixed offline Git argv
            ["git", "diff", "HEAD", "--name-status", *path_args],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_path,
        )
        untracked_result = subprocess.run(  # nosec B603 B607 - fixed offline Git argv
            ["git", "ls-files", "--others", "--exclude-standard", *path_args],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_path,
        )
    except Exception:
        return "Unable to determine file changes"
    changes = []
    if diff_result.stdout.strip():
        changes.extend(("Modified/Deleted:", diff_result.stdout.strip()))
    if untracked_result.stdout.strip():
        changes.extend(("\nUntracked:", untracked_result.stdout.strip()))
    return "\n".join(changes) if changes else "No changes"


async def get_git_diff_summary_async(
    max_chars: int = 8000,
    project_path: str | None = None,
    paths: Sequence[str] | None = None,
) -> str:
    """Get git diff --stat + truncated diff content.

    Provides actual code change context beyond just file names.
    Falls back to staged changes if HEAD diff is empty.

    Args:
        max_chars: Maximum characters for the diff content
        project_path: Optional path to the project directory. When provided,
            git commands run in this directory instead of the current working directory.
        paths: Optional repository paths that bound the returned diff.

    Returns:
        Formatted markdown with stat overview + truncated diff
    """
    path_args = ["--", *paths] if paths else []
    cwd = project_path or Path.cwd()
    stat_raw, diff_raw = await asyncio.gather(
        daemon_git.run(
            ["--literal-pathspecs", "diff", "HEAD", "--stat", *path_args],
            cwd=cwd,
            timeout=10.0,
        ),
        daemon_git.run(
            ["--literal-pathspecs", "diff", "HEAD", *path_args],
            cwd=cwd,
            timeout=10.0,
        ),
    )
    stat_output = _require_git_ok(stat_raw, "Git diff summary").stdout.strip()
    diff_output = _require_git_ok(diff_raw, "Git diff").stdout.strip()

    if not diff_output:
        cached_raw = await daemon_git.run(
            ["--literal-pathspecs", "diff", "--cached", *path_args],
            cwd=cwd,
            timeout=10.0,
        )
        diff_output = _require_git_ok(cached_raw, "Git staged diff").stdout.strip()
        if not stat_output:
            cached_stat_raw = await daemon_git.run(
                ["--literal-pathspecs", "diff", "--cached", "--stat", *path_args],
                cwd=cwd,
                timeout=10.0,
            )
            stat_output = _require_git_ok(cached_stat_raw, "Git staged diff summary").stdout.strip()

    if not stat_output and not diff_output:
        return ""

    sections = []
    if stat_output:
        sections.append(f"### Diff Summary\n```\n{stat_output}\n```")
    if diff_output:
        if len(diff_output) > max_chars:
            diff_output = (
                diff_output[:max_chars]
                + f"\n\n... (truncated, {len(diff_output) - max_chars} chars omitted)"
            )
        sections.append(f"### Actual Changes\n```diff\n{diff_output}\n```")
    return "\n\n".join(sections)


def get_git_diff_summary(
    max_chars: int = 8000,
    project_path: str | None = None,
    paths: Sequence[str] | None = None,
) -> str:
    """Get a diff summary synchronously for offline CLI callers only."""
    path_args = ["--", *paths] if paths else []
    try:
        stat_result = subprocess.run(  # nosec B603 B607 - fixed offline Git argv
            ["git", "diff", "HEAD", "--stat", *path_args],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_path,
        )
        diff_result = subprocess.run(  # nosec B603 B607 - fixed offline Git argv
            ["git", "diff", "HEAD", *path_args],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_path,
        )
        stat_output = stat_result.stdout.strip()
        diff_output = diff_result.stdout.strip()
        if not diff_output:
            diff_result = subprocess.run(  # nosec B603 B607 - fixed offline Git argv
                ["git", "diff", "--cached", *path_args],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=project_path,
            )
            diff_output = diff_result.stdout.strip()
            if not stat_output:
                stat_result = subprocess.run(  # nosec B603 B607 - fixed offline Git argv
                    ["git", "diff", "--cached", "--stat", *path_args],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=project_path,
                )
                stat_output = stat_result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""

    sections = []
    if stat_output:
        sections.append(f"### Diff Summary\n```\n{stat_output}\n```")
    if diff_output:
        if len(diff_output) > max_chars:
            diff_output = (
                diff_output[:max_chars]
                + f"\n\n... (truncated, {len(diff_output) - max_chars} chars omitted)"
            )
        sections.append(f"### Actual Changes\n```diff\n{diff_output}\n```")
    return "\n\n".join(sections)


class DirtyFiles:
    """Categorized dirty files from git status."""

    __slots__ = ("tracked", "untracked")

    def __init__(self, tracked: set[str], untracked: set[str]) -> None:
        self.tracked = tracked
        self.untracked = untracked

    @property
    def all(self) -> set[str]:
        """All dirty files (tracked + untracked)."""
        return self.tracked | self.untracked

    def __bool__(self) -> bool:
        return bool(self.tracked or self.untracked)


def resolve_git_worktree_root(*candidate_paths: str | Path | None) -> str | None:
    """Resolve a Git root for synchronous offline and recovery transactions only."""
    for raw_path in candidate_paths:
        if raw_path is None:
            continue
        path_text = str(raw_path).strip()
        if not path_text or not Path(path_text).is_dir():
            continue
        try:
            result = subprocess.run(  # nosec B603 B607 - fixed offline Git argv
                ["git", "rev-parse", "--show-toplevel"],
                cwd=path_text,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


async def resolve_git_worktree_root_async(
    *candidate_paths: str | Path | None,
) -> str | None:
    """Return the first candidate path that belongs to a git worktree."""
    for raw_path in candidate_paths:
        if raw_path is None:
            continue
        path_text = str(raw_path).strip()
        if not path_text:
            logger.debug("resolve_git_worktree_root: ignoring empty candidate path")
            continue
        if not Path(path_text).is_dir():
            logger.debug("resolve_git_worktree_root: candidate is not a directory: %s", path_text)
            continue

        result = await daemon_git.run(
            ["rev-parse", "--show-toplevel"],
            cwd=path_text,
            timeout=5.0,
        )

        if isinstance(result, GitFailed) and "not a git repository" in result.stderr.lower():
            logger.debug(
                "resolve_git_worktree_root: candidate is not a git worktree: %s",
                path_text,
            )
            continue
        result = _require_git_ok(result, "Git worktree resolution")

        worktree_root = result.stdout.strip()
        if worktree_root:
            return worktree_root

    return None


async def get_dirty_files_async(
    project_path: str | None = None,
    *,
    timeout: float = DEFAULT_GIT_STATUS_TIMEOUT_SECONDS,
) -> set[str]:
    """
    Get the set of dirty files from git status --porcelain.

    Excludes .gobby/ files from the result.

    Args:
        project_path: Path to the project directory
        timeout: Seconds to allow the git subprocess

    Returns:
        Set of dirty file paths (relative to repo root)
    """
    return (await get_dirty_files_categorized_async(project_path, timeout=timeout)).all


def get_dirty_files(
    project_path: str | None = None,
    *,
    timeout: float = DEFAULT_GIT_STATUS_TIMEOUT_SECONDS,
) -> set[str]:
    """Get dirty paths synchronously for offline and recovery transactions only."""
    return get_dirty_files_categorized(project_path, timeout=timeout).all


def get_dirty_files_categorized(
    project_path: str | None = None,
    *,
    timeout: float = DEFAULT_GIT_STATUS_TIMEOUT_SECONDS,
) -> DirtyFiles:
    """Get categorized dirty paths for offline and recovery transactions only."""
    worktree_root = resolve_git_worktree_root(project_path or Path.cwd())
    if worktree_root is None:
        return DirtyFiles(set(), set())
    try:
        result = subprocess.run(  # nosec B603 B607 - fixed offline Git argv
            [
                "git",
                "--literal-pathspecs",
                "--no-optional-locks",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--",
            ],
            cwd=worktree_root,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitStatusUnavailable(f"Git dirty status unavailable: {exc}") from exc
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip() or f"exit {result.returncode}"
        raise GitStatusUnavailable(f"Git dirty status unavailable: {detail}")

    tracked: set[str] = set()
    untracked: set[str] = set()
    for entry in parse_porcelain_v1_z(os.fsdecode(result.stdout)):
        if entry.path.startswith(".gobby/"):
            continue
        if entry.code == "??":
            untracked.add(entry.path)
        else:
            tracked.add(entry.path)
    return DirtyFiles(tracked, untracked)


async def get_dirty_files_categorized_async(
    project_path: str | None = None,
    *,
    timeout: float = DEFAULT_GIT_STATUS_TIMEOUT_SECONDS,
) -> DirtyFiles:
    """
    Get dirty files from git status, split into tracked and untracked.

    Tracked: modified, staged, deleted, renamed (any status except ??).
    Untracked: new files not yet added to git (??).
    Excludes .gobby/ files from both sets.

    Args:
        project_path: Path to the project directory
        timeout: Seconds to allow the complete Git spawn and execution.

    Returns:
        DirtyFiles with .tracked and .untracked sets
    """
    worktree_root = await resolve_git_worktree_root_async(project_path or Path.cwd())
    if worktree_root is None:
        logger.debug(
            "get_dirty_files: no git worktree resolved for project_path=%r; treating as no-repo",
            project_path,
        )
        return DirtyFiles(set(), set())

    result = _require_git_ok(
        await daemon_git.status(worktree_root, timeout=timeout),
        "Git dirty status",
    )
    tracked: set[str] = set()
    untracked: set[str] = set()
    for entry in parse_porcelain_v1_z(result.stdout):
        if entry.path.startswith(".gobby/"):
            continue
        if entry.code == "??":
            untracked.add(entry.path)
        else:
            tracked.add(entry.path)
    return DirtyFiles(tracked, untracked)


def get_task_session_liveness(
    task_id: str,
    session_task_manager: SessionTaskManager | None,
    session_manager: SessionManager | None,
    exclude_session_id: str | None = None,
) -> bool:
    """
    Check if a task is currently being worked on by an active session.

    Args:
        task_id: The task ID to check
        session_task_manager: Manager to look up session-task links
        session_manager: Manager to check session status
        exclude_session_id: ID of session to exclude from check (e.g. current one)

    Returns:
        True if an active session (status='active') is linked to this task.
    """
    if not session_task_manager or not session_manager:
        return False

    try:
        # Get all sessions linked to this task
        linked_sessions = session_task_manager.get_task_sessions(task_id)

        for link in linked_sessions:
            session_id = link.get("session_id")
            if not session_id or session_id == exclude_session_id:
                continue

            # Check if session is truly active
            session = session_manager.get(session_id)
            if session and session.status == "active":
                return True

        return False
    except Exception as e:
        logger.warning("get_task_session_liveness: Error checking liveness for %s: %s", task_id, e)
        return False
