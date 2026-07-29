"""Task-attributed Git dirty-state helpers shared by close and recovery gates."""

from __future__ import annotations

from gobby.utils.git import is_path_gitignored, run_git_command


def committable_task_paths(paths: set[str], cwd: str) -> set[str]:
    """Remove paths that Git intentionally ignores."""
    return {path for path in paths if not is_path_gitignored(path, cwd)}


def task_dirty_paths(paths: set[str], cwd: str) -> set[str] | None:
    """Return dirty attributed paths, or ``None`` when Git inspection fails."""
    scoped_paths = sorted(paths)
    if not scoped_paths:
        return set()
    status = run_git_command(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            *scoped_paths,
        ],
        cwd=cwd,
        timeout=10,
    )
    if status is None:
        return None
    return {_porcelain_path(line) for line in status.splitlines() if line.strip()}


def _porcelain_path(line: str) -> str:
    path = line[3:].strip() if len(line) > 3 else line.strip()
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[1]
    return path.strip('"')


def has_committable_edits(paths: set[str], cwd: str) -> bool:
    """Return whether attributed committable paths are dirty, failing closed."""
    dirty_paths = task_dirty_paths(paths, cwd)
    return dirty_paths is None or bool(dirty_paths)
