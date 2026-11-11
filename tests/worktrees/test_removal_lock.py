"""Descriptor handling for the tree removal lock."""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from gobby.worktrees import removal_lock

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="the removal lock is POSIX-only"),
]


@pytest.fixture
def opened_fds(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Record every descriptor the lock opens, so a test can check that each one was closed."""
    real_open: Callable[..., int] = os.open
    opened: list[int] = []

    def record_open(
        path: str | os.PathLike[str], flags: int, *args: object, **kwargs: object
    ) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", record_open)
    yield opened


def _assert_closed(fd: int) -> None:
    with pytest.raises(OSError) as closed:
        os.fstat(fd)
    assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize("failure", ["flock", "names"])
def test_acquire_closes_its_descriptor_when_the_lock_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened_fds: list[int],
    failure: str,
) -> None:
    def fail(*_args: object) -> bool:
        raise OSError(errno.EIO, "lock check failed")

    if failure == "flock":
        import fcntl

        monkeypatch.setattr(fcntl, "flock", fail)
    else:
        monkeypatch.setattr(removal_lock, "_names", fail)

    with pytest.raises(OSError, match="lock check failed"):
        removal_lock._acquire(str(tmp_path / ".tree.removal.lock"))

    assert len(opened_fds) == 1
    _assert_closed(opened_fds[0])


def test_release_closes_its_descriptor_when_the_lock_file_cannot_be_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened_fds: list[int],
) -> None:
    def refuse(path: str) -> None:
        raise PermissionError(errno.EACCES, "unlink refused", path)

    monkeypatch.setattr(os, "unlink", refuse)
    # The tree is never created, so release tries to remove the lock file and is refused.
    tree = tmp_path / "tree"

    with removal_lock.hold_tree_removal(str(tree)) as held:
        assert held

    assert len(opened_fds) == 1
    _assert_closed(opened_fds[0])
