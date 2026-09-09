"""Task-attributed Git dirty-state helpers shared by close and recovery gates."""

from __future__ import annotations

from gobby.utils.daemon_git import GitOk, daemon_git, parse_porcelain_v1_z
from gobby.utils.git import is_path_gitignored, run_git_command


def committable_task_paths(paths: set[str], cwd: str) -> set[str]:
    """Remove paths that Git intentionally ignores."""
    return {path for path in paths if not is_path_gitignored(path, cwd)}


def task_dirty_paths(paths: set[str], cwd: str) -> set[str] | None:
    """Inspect dirty paths synchronously for close transactions only."""
    scoped_paths = sorted(paths)
    if not scoped_paths:
        return set()
    status = run_git_command(
        [
            "git",
            "--literal-pathspecs",
            "--no-optional-locks",
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


async def task_dirty_paths_async(paths: set[str], cwd: str) -> set[str] | None:
    """Return dirty attributed paths without blocking the daemon runtime."""
    if not paths:
        return set()
    result = await daemon_git.status(cwd, paths=paths, timeout=10.0)
    if not isinstance(result, GitOk):
        return None
    return {entry.path for entry in parse_porcelain_v1_z(result.stdout)}


def _porcelain_path(line: str) -> str:
    # ``run_git_command`` strips stdout, so a first line whose XY status starts
    # with a space (" M path") arrives as "M path" with one status char.
    if len(line) >= 3 and line[2] == " ":
        path = line[3:].strip()
    elif len(line) >= 2 and line[1] == " ":
        path = line[2:].strip()
    else:
        path = line.strip()
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[1]
    return path.strip('"')


def has_committable_edits(paths: set[str], cwd: str) -> bool:
    """Return whether attributed committable paths are dirty, failing closed."""
    dirty_paths = task_dirty_paths(paths, cwd)
    return dirty_paths is None or bool(dirty_paths)
