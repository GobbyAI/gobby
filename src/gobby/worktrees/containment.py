"""Which worktree a path lives in, resolved on the machine that owns both."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol


class WorktreeLike(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def worktree_path(self) -> str: ...


type WorktreeRoots = list[tuple[Path, str]]


def path_is_within(path: str, root: str) -> bool:
    """Return whether path is root or one of its descendants."""
    try:
        Path(path).expanduser().resolve().relative_to(Path(root).expanduser().resolve())
    except (OSError, ValueError):
        return False
    return True


def worktree_roots(worktrees: Iterable[WorktreeLike]) -> WorktreeRoots:
    """Each worktree's checkout resolved on this machine, deepest first."""
    roots: WorktreeRoots = []
    for worktree in worktrees:
        if not worktree.worktree_path:
            continue
        try:
            roots.append((Path(worktree.worktree_path).expanduser().resolve(), worktree.id))
        except OSError:
            continue
    return sorted(roots, key=lambda root: len(root[0].parts), reverse=True)


def containing_worktree_id(path: str, roots: WorktreeRoots) -> str | None:
    """The deepest worktree holding `path` once it resolves on this machine.

    Symlinks and `..` resolve before the comparison, so a link inside a
    worktree that points outside it does not bind to it (#23280).
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return None
    return next((worktree_id for root, worktree_id in roots if resolved.is_relative_to(root)), None)
