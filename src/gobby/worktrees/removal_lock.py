"""Per-tree removal lock shared with the gdaemon removal path.

A removal holds ``flock(LOCK_EX)`` on ``<real parent>/.<tree name>.removal.lock`` while it works
on the tree. gdaemon's ``workspace::remove`` takes the same lock on the same path, so a removal
that finds the lock busy keeps its record instead of reading a half-removed tree as dirty.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

if sys.platform != "win32":  # pragma: no branch - POSIX platforms share fcntl
    import fcntl


@contextmanager
def hold_tree_removal(worktree_path: str | os.PathLike[str]) -> Iterator[bool]:
    """Hold the tree's removal lock for the block; yield False when another remover holds it."""
    if sys.platform == "win32":  # pragma: no cover - gdaemon's node path is POSIX-only
        yield True
        return
    tree = Path(worktree_path)
    parent = os.path.realpath(tree.parent)
    if not os.path.isdir(parent):
        # No parent directory means no tree to race for.
        yield True
        return
    lock_path = os.path.join(parent, f".{tree.name}.removal.lock")
    fd = _acquire(lock_path)
    if fd is None:
        yield False
        return
    try:
        yield True
    finally:
        try:
            # Unlink while still locked, and only once the tree is gone. A waiter that opened the
            # unlinked file sees the name move on in `_acquire` and locks the replacement instead.
            # The unlink is best effort: a lock file left behind is harmless, and a failure here
            # must not fail a removal that already finished.
            if not os.path.lexists(tree):
                with suppress(OSError):
                    os.unlink(lock_path)
        finally:
            os.close(fd)


def _acquire(lock_path: str) -> int | None:
    """Return a descriptor holding the lock, or None when another remover holds it."""
    while True:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        locked = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            locked = _names(fd, lock_path)
            if locked:
                return fd
        finally:
            # Every path that does not hand the descriptor to the caller closes it, including an
            # unexpected error from the lock or the identity check.
            if not locked:
                os.close(fd)


def _names(fd: int, lock_path: str) -> bool:
    """Whether the path still names the file this descriptor locked."""
    try:
        current = os.stat(lock_path)
    except FileNotFoundError:
        return False
    held = os.fstat(fd)
    return (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino)
