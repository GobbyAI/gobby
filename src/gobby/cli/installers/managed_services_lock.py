"""Cross-process lifecycle lock for hub-managed datastores."""

from __future__ import annotations

import errno
import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol, cast


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
else:  # pragma: no cover - POSIX only
    _msvcrt = None


class ManagedServicesLockError(RuntimeError):
    """Raised when a managed-service lifecycle transition cannot acquire its lock."""


@dataclass
class _HeldLock:
    path: Path
    file: IO[str]
    depth: int = 1


_thread_state = threading.local()


@contextmanager
def managed_services_lock(
    gobby_home: Path,
    *,
    operation: str,
    timeout: float = 30.0,
) -> Iterator[None]:
    """Serialize hub-local managed-service transitions with thread re-entrancy."""
    path = gobby_home.expanduser() / "managed-services.lock"
    held = getattr(_thread_state, "managed_services_lock", None)
    if isinstance(held, _HeldLock):
        if held.path != path:
            raise ManagedServicesLockError(
                f"managed-services lock already held for {held.path.parent}"
            )
        held.depth += 1
        try:
            yield
        finally:
            held.depth -= 1
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+", encoding="utf-8")
    path.chmod(0o600)
    deadline = time.monotonic() + timeout
    while not _try_acquire(lock_file):
        if time.monotonic() >= deadline:
            holder = _read_holder(path)
            lock_file.close()
            raise ManagedServicesLockError(
                f"Timed out after {timeout:g}s waiting for managed-services lock; holder: {holder}"
            )
        time.sleep(0.05)

    state = _HeldLock(path=path, file=lock_file)
    _thread_state.managed_services_lock = state
    _write_holder(lock_file, operation)
    try:
        yield
    finally:
        _thread_state.managed_services_lock = None
        _clear_holder(lock_file)
        _release(lock_file)
        lock_file.close()


def _try_acquire(lock_file: IO[str]) -> bool:
    try:
        if _fcntl is not None:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        elif _msvcrt is not None:  # pragma: no cover - Windows only
            lock_file.seek(0)
            if not lock_file.read(1):
                lock_file.write(" ")
                lock_file.flush()
            lock_file.seek(0)
            _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - unsupported platform
            raise ManagedServicesLockError("No supported file-locking implementation")
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def _release(lock_file: IO[str]) -> None:
    if _fcntl is not None:
        _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
    elif _msvcrt is not None:  # pragma: no cover - Windows only
        lock_file.seek(0)
        _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_UNLCK, 1)


def _write_holder(lock_file: IO[str], operation: str) -> None:
    payload = {
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
        "operation": operation,
        "acquired_at": time.time(),
    }
    lock_file.seek(0)
    lock_file.truncate()
    json.dump(payload, lock_file, sort_keys=True)
    lock_file.flush()
    os.fsync(lock_file.fileno())


def _clear_holder(lock_file: IO[str]) -> None:
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.flush()


def _read_holder(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return f"unreadable ({exc})"
    return value or "unknown"
