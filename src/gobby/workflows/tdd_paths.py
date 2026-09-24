"""Resolve a TDD path against the git checkout that contains it."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from functools import lru_cache
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
    if not start.exists() or not project.exists():
        return None
    worktree_top, worktree_common = _git_identity(str(start.resolve()))
    _project_top, project_common = _git_identity(str(project.resolve()))
    if not worktree_top or not worktree_common or worktree_common != project_common:
        return None
    return Path(worktree_top)


@lru_cache(maxsize=512)
def _git_identity(directory: str) -> tuple[str | None, str | None]:
    """Return (toplevel, common dir) for one directory, from a single git call."""
    text = _git_out(
        Path(directory),
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-common-dir",
    )
    if text is None:
        return (None, None)
    lines = text.splitlines()
    if len(lines) != 2:
        return (None, None)
    return (lines[0], lines[1])


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
