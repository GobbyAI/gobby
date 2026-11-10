"""Descriptor-relative directory walks that never follow a symbolic link."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

NO_FOLLOW_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def open_nofollow_dir(
    root: Path, parts: Sequence[str], *, create: bool = False, mode: int = 0o777
) -> int:
    """Return a descriptor for ``root/parts`` opened one component at a time.

    Each component is opened relative to its parent's descriptor with
    O_NOFOLLOW|O_DIRECTORY, so a symlinked root or component raises (ELOOP or
    ENOTDIR) instead of being traversed, and a component swapped after it was
    opened cannot redirect later operations on the returned descriptor. With
    ``create``, missing components are created relative to the held parent.
    The caller owns the returned descriptor.
    """
    for part in parts:
        if part in ("", ".", "..") or "/" in part:
            raise ValueError(f"Refusing a non-local path component: {part!r}")
    fd = os.open(root, NO_FOLLOW_DIR)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, NO_FOLLOW_DIR, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd
