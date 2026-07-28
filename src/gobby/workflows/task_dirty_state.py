"""Task-attributed Git dirty-state helpers shared by close and recovery gates."""

from __future__ import annotations

from gobby.utils.git import is_path_gitignored, run_git_command


def committable_task_paths(paths: set[str], cwd: str) -> set[str]:
    """Remove paths that Git intentionally ignores."""
    return {path for path in paths if not is_path_gitignored(path, cwd)}


def task_dirty_paths(paths: set[str], cwd: str) -> set[str] | None:
    """Return dirty attributed paths, or ``None`` when Git inspection fails."""
    dirty_paths: set[str] = set()
    for path in sorted(committable_task_paths(paths, cwd)):
        status = run_git_command(
            ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", path],
            cwd=cwd,
            timeout=10,
        )
        if status is None:
            return None
        if status.strip():
            dirty_paths.add(path)
    return dirty_paths


def has_committable_edits(paths: set[str], cwd: str) -> bool:
    """Return whether attributed committable paths are dirty, failing closed."""
    dirty_paths = task_dirty_paths(paths, cwd)
    return dirty_paths is None or bool(dirty_paths)
