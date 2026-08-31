"""Git utility functions for workflow actions.

Extracted from actions.py as part of strangler fig decomposition.
These are pure utility functions with no ActionContext dependency.
"""

from __future__ import annotations

import logging
import os
import subprocess  # nosec B404 # subprocess needed for git commands
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.storage.session_tasks import SessionTaskManager
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

# ``git status --porcelain=v2 -z`` costs tens of milliseconds on a healthy repo,
# so this is headroom for a loaded machine rather than a working budget. It is
# deliberately well inside the shared blocking-effect budget: a caller parked
# here holds one of the workflow runtime's few blocking threads.
DEFAULT_GIT_STATUS_TIMEOUT_SECONDS = 5.0


def get_git_status(project_path: str | None = None) -> str:
    """Get git status for a project directory.

    Args:
        project_path: Optional path to the project directory.

    Returns:
        Short git status output, or error message if not a git repo.
    """
    try:
        result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "status", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_path,
        )
        return result.stdout.strip() or "No changes"
    except Exception:
        return "Not a git repository or git not available"


def get_recent_git_commits(
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
    try:
        result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "log", f"-{max_commits}", "--format=%H|%s"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_path,
        )
        if result.returncode != 0:
            return []

        commits = []
        for line in result.stdout.strip().split("\n"):
            if "|" in line:
                hash_part, message = line.split("|", 1)
                commits.append({"hash": hash_part, "message": message})
        return commits
    except Exception:
        return []


def get_file_changes(
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
    try:
        # Get changed files with status
        path_args = ["--", *paths] if paths else []
        diff_result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "diff", "HEAD", "--name-status", *path_args],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_path,
        )

        # Get untracked files
        untracked_result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "ls-files", "--others", "--exclude-standard", *path_args],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_path,
        )

        # Combine results
        changes = []
        if diff_result.stdout.strip():
            changes.append("Modified/Deleted:")
            changes.append(diff_result.stdout.strip())

        if untracked_result.stdout.strip():
            changes.append("\nUntracked:")
            changes.append(untracked_result.stdout.strip())

        return "\n".join(changes) if changes else "No changes"

    except Exception:
        return "Unable to determine file changes"


def get_git_diff_summary(
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
    try:
        # Get stat overview
        path_args = ["--", *paths] if paths else []
        stat_result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "diff", "HEAD", "--stat", *path_args],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_path,
        )
        stat_output = stat_result.stdout.strip()

        # Get actual diff content
        diff_result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "diff", "HEAD", *path_args],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_path,
        )
        diff_output = diff_result.stdout.strip()

        # Fall back to staged changes if HEAD diff is empty
        if not diff_output:
            diff_result = subprocess.run(  # nosec B603 B607 # hardcoded git command
                ["git", "diff", "--cached", *path_args],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=project_path,
            )
            diff_output = diff_result.stdout.strip()
            if not stat_output:
                stat_result = subprocess.run(  # nosec B603 B607 # hardcoded git command
                    ["git", "diff", "--cached", "--stat", *path_args],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=project_path,
                )
                stat_output = stat_result.stdout.strip()

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

    except (subprocess.TimeoutExpired, OSError):
        logger.debug("get_git_diff_summary failed", exc_info=True)
        return ""


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

        try:
            result = subprocess.run(  # nosec B603 B607 # hardcoded git command
                ["git", "rev-parse", "--show-toplevel"],
                cwd=path_text,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception as exc:
            logger.debug(
                "resolve_git_worktree_root: could not inspect candidate %s: %s",
                path_text,
                exc,
            )
            continue

        if result.returncode != 0:
            logger.debug(
                "resolve_git_worktree_root: candidate is not a git worktree: %s",
                path_text,
            )
            continue

        worktree_root = result.stdout.strip()
        if worktree_root:
            return worktree_root

    return None


def get_dirty_files(
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
    return get_dirty_files_categorized(project_path, timeout=timeout).all


def get_dirty_files_categorized(
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
        timeout: Seconds to allow the git subprocess. Callers on a deadline pass
            their remaining budget; a timeout reports a clean tree, so keep a
            floor rather than letting a spent budget reach zero.

    Returns:
        DirtyFiles with .tracked and .untracked sets
    """
    worktree_root = resolve_git_worktree_root(project_path)
    if worktree_root is None:
        logger.debug(
            "get_dirty_files: no git worktree resolved for project_path=%r; treating as no-repo",
            project_path,
        )
        return DirtyFiles(set(), set())

    try:
        result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "status", "--porcelain=v2", "-z"],
            cwd=worktree_root,
            capture_output=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            logger.warning("get_dirty_files: git status failed: %s", stderr)
            return DirtyFiles(set(), set())

        tracked: set[str] = set()
        untracked: set[str] = set()
        records = result.stdout.split(b"\0")
        record_index = 0
        while record_index < len(records):
            record = records[record_index]
            record_index += 1
            if not record:
                continue

            record_type = record[:1]
            if record_type == b"1":
                fields = record.split(b" ", 8)
                path = fields[8] if len(fields) == 9 else None
            elif record_type == b"2":
                fields = record.split(b" ", 9)
                path = fields[9] if len(fields) == 10 else None
                record_index += 1  # Porcelain v2 stores the original path next.
            elif record_type == b"u":
                fields = record.split(b" ", 10)
                path = fields[10] if len(fields) == 11 else None
            elif record_type == b"?":
                path = record[2:]
            else:
                continue

            if path is None:
                continue
            filepath = os.fsdecode(path)
            if filepath.startswith(".gobby/"):
                continue
            if record_type == b"?":
                untracked.add(filepath)
            else:
                tracked.add(filepath)

        return DirtyFiles(tracked, untracked)

    except subprocess.TimeoutExpired:
        # Reports a clean tree, so say which budget produced it — a dirty-file
        # gate that silently stops gating is otherwise indistinguishable here.
        logger.warning("get_dirty_files: git status timed out after %.1fs", timeout)
        return DirtyFiles(set(), set())
    except FileNotFoundError:
        logger.warning(
            "get_dirty_files: git binary not found or cwd invalid (cwd=%s)", worktree_root
        )
        return DirtyFiles(set(), set())
    except Exception as e:
        logger.error("get_dirty_files: Error running git status: %s", e)
        return DirtyFiles(set(), set())


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
