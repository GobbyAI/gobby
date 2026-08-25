"""Locked, atomic filesystem helpers for JSONL exports."""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from gobby import paths


def project_backup_path(project_id: str, filename: str) -> Path:
    """Return the machine-local backup path for a committed project UUID."""
    return paths.get_gobby_home() / "backups" / project_id / filename


if sys.platform == "win32":  # pragma: no cover - Windows only
    import msvcrt
else:  # pragma: no branch - POSIX platforms share fcntl
    import fcntl


@contextmanager
def export_file_lock(path: Path) -> Iterator[None]:
    """Hold a blocking inter-process lock for an export target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":  # pragma: no cover - Windows only
            os.ftruncate(fd, 1)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if sys.platform == "win32":  # pragma: no cover - Windows only
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def atomic_write_text(path: Path, content: str) -> None:
    """Replace a text file atomically after flushing its new contents to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=os.fspath(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise
