"""Exclusive PID-file ownership for the daemon process."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Protocol, cast


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


class PidFileClaim:
    """A held advisory lock protecting a daemon PID file."""

    def __init__(self, lock_path: Path, lock_fd: int) -> None:
        self.lock_path = lock_path
        self._lock_fd = lock_fd
        self._released = False

    def release(self) -> None:
        """Release the held lock without modifying the PID file."""
        if self._released:
            return
        try:
            _unlock_file(self._lock_fd)
        finally:
            os.close(self._lock_fd)
            self._released = True


def _lock_file(lock_fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        return
    if _msvcrt is not None:  # pragma: no cover - Windows only
        os.lseek(lock_fd, 0, os.SEEK_SET)
        _msvcrt.locking(lock_fd, _msvcrt.LK_NBLCK, 1)
        return
    raise OSError("no supported file locking implementation")  # pragma: no cover


def _unlock_file(lock_fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
    elif _msvcrt is not None:  # pragma: no cover - Windows only
        os.lseek(lock_fd, 0, os.SEEK_SET)
        _msvcrt.locking(lock_fd, _msvcrt.LK_UNLCK, 1)


def _read_pid(pid_file: Path) -> int | None:
    try:
        pid = int(pid_file.read_text().strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def claim_pid_file(pid_file: Path) -> PidFileClaim | None:
    """Claim and write ``pid_file``, or return ``None`` when another process owns it."""
    lock_path = pid_file.with_name(f"{pid_file.name}.lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            _lock_file(lock_fd)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                os.close(lock_fd)
                return None
            raise

        stored_pid = _read_pid(pid_file)
        if stored_pid is not None and stored_pid != os.getpid() and _pid_is_alive(stored_pid):
            _unlock_file(lock_fd)
            os.close(lock_fd)
            return None

        pid_fd = os.open(pid_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(pid_fd, str(os.getpid()).encode())
            os.fsync(pid_fd)
        finally:
            os.close(pid_fd)
    except BaseException:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        raise

    return PidFileClaim(lock_path, lock_fd)


__all__ = ["PidFileClaim", "claim_pid_file"]
