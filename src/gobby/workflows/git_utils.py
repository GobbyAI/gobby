"""Git utility functions for workflow actions.

Extracted from actions.py as part of strangler fig decomposition.
These are pure utility functions with no ActionContext dependency.
"""

from __future__ import annotations

import logging
import subprocess  # nosec B404 # subprocess needed for git commands
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.storage.session_tasks import SessionTaskManager
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


def get_git_status() -> str:
    """Get git status for current directory.

    Returns:
        Short git status output, or error message if not a git repo.
    """
    try:
        result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "status", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or "No changes"
    except Exception:
        return "Not a git repository or git not available"


def get_recent_git_commits(max_commits: int = 10) -> list[dict[str, str]]:
    """Get recent git commits with hash and message.

    Args:
        max_commits: Maximum number of commits to return

    Returns:
        List of dicts with 'hash' and 'message' keys
    """
    try:
        result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "log", f"-{max_commits}", "--format=%H|%s"],
            capture_output=True,
            text=True,
            timeout=5,
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


def get_file_changes() -> str:
    """Get detailed file changes from git.

    Returns:
        Formatted string with modified/deleted and untracked files.
    """
    try:
        # Get changed files with status
        diff_result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "diff", "HEAD", "--name-status"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        # Get untracked files
        untracked_result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=5,
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


def get_git_diff_summary(max_chars: int = 8000, project_path: str | None = None) -> str:
    """Get git diff --stat + truncated diff content.

    Provides actual code change context beyond just file names.
    Falls back to staged changes if HEAD diff is empty.

    Args:
        max_chars: Maximum characters for the diff content
        project_path: Optional path to the project directory. When provided,
            git commands run in this directory instead of the current working directory.

    Returns:
        Formatted markdown with stat overview + truncated diff
    """
    try:
        # Get stat overview
        stat_result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "diff", "HEAD", "--stat"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_path,
        )
        stat_output = stat_result.stdout.strip()

        # Get actual diff content
        diff_result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "diff", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_path,
        )
        diff_output = diff_result.stdout.strip()

        # Fall back to staged changes if HEAD diff is empty
        if not diff_output:
            diff_result = subprocess.run(  # nosec B603 B607 # hardcoded git command
                ["git", "diff", "--cached"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=project_path,
            )
            diff_output = diff_result.stdout.strip()
            if not stat_output:
                stat_result = subprocess.run(  # nosec B603 B607 # hardcoded git command
                    ["git", "diff", "--cached", "--stat"],
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


def get_dirty_files(project_path: str | None = None) -> set[str]:
    """
    Get the set of dirty files from git status --porcelain.

    Excludes .gobby/ files from the result.

    Args:
        project_path: Path to the project directory

    Returns:
        Set of dirty file paths (relative to repo root)
    """
    return get_dirty_files_categorized(project_path).all


def get_dirty_files_categorized(project_path: str | None = None) -> DirtyFiles:
    """
    Get dirty files from git status, split into tracked and untracked.

    Tracked: modified, staged, deleted, renamed (any status except ??).
    Untracked: new files not yet added to git (??).
    Excludes .gobby/ files from both sets.

    Args:
        project_path: Path to the project directory

    Returns:
        DirtyFiles with .tracked and .untracked sets
    """
    # Normalize empty string to None — "" is falsy but subprocess.run(cwd="")
    # raises FileNotFoundError. Treat it the same as None (use daemon's cwd).
    if project_path is not None and not project_path.strip():
        # Include session/project context for diagnosis if available.
        diag = ""
        try:
            from gobby.utils.session_context import get_current_session_id

            sid = get_current_session_id()
            if sid:
                diag += f" session={sid}"
            from gobby.utils.project_context import _current_project_context

            ctx = _current_project_context.get()
            if ctx:
                diag += f" project={ctx.get('id', '?')} name={ctx.get('name', '?')}"
        except Exception:
            pass
        logger.warning(
            f"get_dirty_files: called with empty string cwd — normalizing to None.{diag}",
            stack_info=True,
        )
        project_path = None

    if project_path is None:
        logger.debug(
            "get_dirty_files: project_path is None, git status will use daemon's cwd "
            "which may not be the project directory"
        )

    # Validate cwd exists before shelling out — subprocess.run raises
    # FileNotFoundError for both missing binary AND missing cwd, and the
    # latter is the common case (stale session with deleted project path).
    if project_path and not Path(project_path).is_dir():
        logger.debug(f"get_dirty_files: project_path does not exist: {project_path}")
        return DirtyFiles(set(), set())

    try:
        result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "status", "--porcelain"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            logger.warning(f"get_dirty_files: git status failed: {result.stderr}")
            return DirtyFiles(set(), set())

        tracked = set()
        untracked = set()
        # Split by newline first, don't strip() the whole string as it removes
        # the leading space from git status format (e.g., " M file.py")
        for line in result.stdout.split("\n"):
            line = line.rstrip()  # Remove trailing whitespace only
            if not line:
                continue
            # Format is "XY filename" or "XY filename -> newname" for renames
            status = line[:2]
            # Skip the status prefix (first 3 chars: 2 status chars + space)
            filepath = line[3:].split(" -> ")[0]  # Handle renames
            # Exclude .gobby/ files
            if filepath.startswith(".gobby/"):
                continue
            if status == "??":
                untracked.add(filepath)
            else:
                tracked.add(filepath)

        return DirtyFiles(tracked, untracked)

    except subprocess.TimeoutExpired:
        logger.warning("get_dirty_files: git status timed out")
        return DirtyFiles(set(), set())
    except FileNotFoundError:
        logger.warning(f"get_dirty_files: git binary not found or cwd invalid (cwd={project_path})")
        return DirtyFiles(set(), set())
    except Exception as e:
        logger.error(f"get_dirty_files: Error running git status: {e}")
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
        logger.warning(f"get_task_session_liveness: Error checking liveness for {task_id}: {e}")
        return False
