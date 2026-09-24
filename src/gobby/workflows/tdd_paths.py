"""Resolve a TDD path against the git checkout that contains it."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path


def tdd_path_identity(path: str, root: Path | None, normalize: Callable[[str], str]) -> str:
    """Return the repo-relative form of path, or its normalized absolute form."""
    candidate = Path(path)
    if root is not None:
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if resolved.is_relative_to(root):
            return resolved.relative_to(root).as_posix()
        worktree = same_repo_worktree_root(resolved, root)
        if worktree is not None and resolved.is_relative_to(worktree):
            return resolved.relative_to(worktree).as_posix()
    return normalize(path).rstrip("/")


def same_repo_worktree_root(resolved: Path, project: Path) -> Path | None:
    """Return the worktree root when resolved belongs to project's git repo."""
    start = resolved if resolved.is_dir() else resolved.parent
    if not start.exists():
        return None
    toplevel = _git_out(start, "rev-parse", "--show-toplevel")
    if toplevel is None:
        return None
    worktree = Path(toplevel)
    project_common = _git_out(project, "rev-parse", "--path-format=absolute", "--git-common-dir")
    worktree_common = _git_out(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not project_common or project_common != worktree_common:
        return None
    return worktree


def _git_out(cwd: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None
