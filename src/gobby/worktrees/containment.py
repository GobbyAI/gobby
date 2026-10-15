"""Which worktree a path lives in, resolved on the machine that owns both."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol


class WorktreeLike(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def worktree_path(self) -> str: ...


type WorktreeRoots = Mapping[Path, str]


def path_is_within(path: str, root: str) -> bool:
    """Return whether path is root or one of its descendants."""
    try:
        Path(path).expanduser().resolve().relative_to(Path(root).expanduser().resolve())
    except (OSError, ValueError):
        return False
    return True


def worktree_roots(worktrees: Iterable[WorktreeLike]) -> WorktreeRoots:
    """Each worktree's checkout resolved on this machine; the first listed wins a shared path."""
    roots: dict[Path, str] = {}
    for worktree in worktrees:
        if not worktree.worktree_path:
            continue
        try:
            roots.setdefault(Path(worktree.worktree_path).expanduser().resolve(), worktree.id)
        except OSError:
            continue
    return roots


def containing_worktree_id(path: str, roots: WorktreeRoots) -> str | None:
    """The deepest worktree holding `path` once it resolves on this machine.

    Symlinks and `..` resolve before the comparison, so a link inside a
    worktree that points outside it does not bind to it (#23280). Probing the
    path's own ancestors, deepest first, keeps a lookup independent of how
    many roots the project has (#23359).
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return None
    return next(
        (
            worktree_id
            for root in (resolved, *resolved.parents)
            if (worktree_id := roots.get(root)) is not None
        ),
        None,
    )
