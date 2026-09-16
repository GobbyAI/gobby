"""Inter-process file locking and durable atomic replacement helpers."""

from __future__ import annotations

import errno
import math
import os
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":  # pragma: no cover - Windows only
    import msvcrt
else:  # pragma: no branch - POSIX platforms share fcntl
    import fcntl


class DurableFileError(OSError):
    """Raised when a durable replacement cannot be verified."""


@contextmanager
def exclusive_file_lock(path: Path, *, timeout_seconds: float | None = None) -> Iterator[None]:
    """Hold an owner-only sidecar flock for ``path``."""
    if timeout_seconds is not None and (not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise TimeoutError("durable file lock deadline exceeded")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        if sys.platform == "win32":  # pragma: no cover - Windows only
            # msvcrt locks a byte range from the current offset; pin one real byte.
            os.ftruncate(lock_fd, 1)
            os.lseek(lock_fd, 0, os.SEEK_SET)
        if sys.platform != "win32" and timeout_seconds is None:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        else:
            cutoff = None if timeout_seconds is None else time.monotonic() + timeout_seconds
            while not _try_exclusive_lock(lock_fd):
                if cutoff is None:
                    time.sleep(0.01)
                    continue
                remaining = cutoff - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("durable file lock deadline exceeded")
                time.sleep(min(0.01, remaining))
        try:
            yield
        finally:
            if sys.platform == "win32":  # pragma: no cover - Windows only
                os.lseek(lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(lock_fd)


def _try_exclusive_lock(lock_fd: int) -> bool:
    """Take the sidecar lock without blocking; False means another holder has it."""
    if sys.platform == "win32":  # pragma: no cover - Windows only
        try:
            msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            # Contention surfaces as EACCES/EDEADLOCK; anything else is a real failure.
            if exc.errno in (errno.EACCES, errno.EDEADLOCK):
                return False
            raise
        return True
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def durable_replace(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Atomically replace a file and durably verify its exact contents.

    The temporary file and destination directory are both fsynced. A readback
    after the rename prevents callers from advancing external journals behind
    a missing or corrupted identity anchor.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        try:
            os.fchmod(fd, mode)
            remaining = memoryview(content)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise DurableFileError(f"Failed to write temporary file for {path}")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

        os.replace(temp_path, path)
        # Windows cannot open a directory as an fd; NTFS journals the rename itself.
        if sys.platform != "win32":  # pragma: no branch - POSIX fsyncs the directory entry
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(path.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

        if path.read_bytes() != content:
            raise DurableFileError(f"Durable replacement readback mismatch for {path}")
    finally:
        temp_path.unlink(missing_ok=True)


def durable_replace_files_home(source: Path, final_locator: str, temp_locator: str) -> None:
    """Publish streamed bytes through the held files_home fd without mkdir of the root."""
    from gobby.paths import (
        assert_held_files_home_identity,
        ensure_files_home_descendant_dir,
        fsync_files_home_descendant_dir,
        open_files_home_descendant,
        replace_files_home_descendant,
        require_files_home,
        unlink_files_home_descendant,
    )

    require_files_home()
    assert_held_files_home_identity()
    parent = str(Path(final_locator).parent)
    if parent not in {"", "."}:
        ensure_files_home_descendant_dir(parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = open_files_home_descendant(temp_locator, flags, create_parents=True)
    try:
        with source.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                remaining = memoryview(chunk)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise DurableFileError(
                            f"Failed to write temporary file for {final_locator}"
                        )
                    remaining = remaining[written:]
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            unlink_files_home_descendant(temp_locator)
        except FileNotFoundError:
            pass
        raise
    else:
        os.close(fd)

    replace_files_home_descendant(temp_locator, final_locator)
    if parent not in {"", "."}:
        fsync_files_home_descendant_dir(parent)
    else:
        from gobby.paths import files_home_root_fd

        os.fsync(files_home_root_fd())
    verify_fd = open_files_home_descendant(final_locator, os.O_RDONLY)
    try:
        if os.fstat(verify_fd).st_size != source.stat().st_size:
            raise DurableFileError(f"Durable replacement readback mismatch for {final_locator}")
        assert_held_files_home_identity()
    finally:
        os.close(verify_fd)


def durable_replace_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Durably replace ``path`` with UTF-8 text."""
    durable_replace(path, content.encode(), mode=mode)


__all__ = [
    "DurableFileError",
    "durable_replace",
    "durable_replace_files_home",
    "durable_replace_text",
    "exclusive_file_lock",
]
