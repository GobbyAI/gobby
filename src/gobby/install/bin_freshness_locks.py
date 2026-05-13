"""Non-blocking file locks for managed native binary updates."""

from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
from typing import Protocol, cast

logger = logging.getLogger(__name__)


class _FcntlModule(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, fd: int, operation: int) -> None: ...


class _MsvcrtModule(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, fd: int, mode: int, nbytes: int) -> None: ...


if os.name == "posix":  # pragma: no cover - platform dependent
    import fcntl as _fcntl_import

    _fcntl: _FcntlModule | None = _fcntl_import
else:  # pragma: no cover - Windows only
    _fcntl = None

if os.name == "nt":  # pragma: no cover - Windows only
    import msvcrt as _msvcrt_import

    _msvcrt: _MsvcrtModule | None = cast(_MsvcrtModule, _msvcrt_import)
else:  # pragma: no cover - Unix only
    _msvcrt = None


class NativeBinFileLock:
    """A held per-tool native binary update lock."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self._fd = fd
        self._closed = False

    def release(self) -> None:
        """Release the held lock. Idempotent and tolerant of stale fds."""
        if self._closed:
            return
        try:
            if _fcntl is not None:
                _fcntl.flock(self._fd, _fcntl.LOCK_UN)
            elif _msvcrt is not None:  # pragma: no cover - Windows only
                os.lseek(self._fd, 0, os.SEEK_SET)
                _msvcrt.locking(self._fd, _msvcrt.LK_UNLCK, 1)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                self._closed = True
                raise
            logger.debug("Lock fd already closed for %s; treating release as a no-op", self.path)
        finally:
            try:
                os.close(self._fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    self._closed = True
                    raise
            self._closed = True

    def __enter__(self) -> NativeBinFileLock:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


def try_acquire_native_bin_lock(tool_name: str, *, bin_dir: Path) -> NativeBinFileLock | None:
    """Try to acquire a per-tool update lock without blocking."""
    lock_dir = bin_dir / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{tool_name}.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        elif _msvcrt is not None:  # pragma: no cover - Windows only
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - no known supported platform
            raise OSError("no supported file locking implementation")
    except OSError as exc:
        os.close(fd)
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return None
        raise
    return NativeBinFileLock(lock_path, fd)


__all__ = ["NativeBinFileLock", "try_acquire_native_bin_lock"]
