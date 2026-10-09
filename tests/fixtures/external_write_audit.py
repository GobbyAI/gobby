"""Attribute Python filesystem mutations to the isolated E2E writer."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT_ENV = "GOBBY_E2E_EXTERNAL_WRITE_ROOT"
LOG_ENV = "GOBBY_E2E_EXTERNAL_WRITE_LOG"


@dataclass(eq=False)
class WriteAudit:
    root: Path
    log: Path | None = None
    writes: list[str] = field(default_factory=list)


_active: list[WriteAudit] = []
_installed = False


def _mutations(event: str, args: tuple[object, ...]) -> list[tuple[object, object]]:
    if event == "sqlite3.connect":
        database = args[0]
        if database == ":memory:":
            return []
        if isinstance(database, str) and database.startswith("file:"):
            uri = urlsplit(database)
            if parse_qs(uri.query).get("mode") in (["ro"], ["memory"]):
                return []
            database = unquote(uri.path)
        return [(database, None)]
    if event == "open":
        path, mode, flags = args
        writable = isinstance(mode, str) and any(char in mode for char in "wax+")
        writable |= isinstance(flags, int) and bool(
            flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        )
        return [(path, None)] if writable else []
    if event in {"os.remove", "os.rmdir"}:
        return [(args[0], args[1])]
    if event in {"os.mkdir", "os.chmod", "os.chown"}:
        return [(args[0], args[-1])]
    if event in {"os.rename", "os.link"}:
        return [(args[0], args[2]), (args[1], args[3])]
    if event == "os.symlink":
        return [(args[1], args[2])]
    if event == "os.utime":
        return [(args[0], args[3])]
    if event == "os.truncate":
        return [(args[0], None)]
    return []


def _descriptor_path(descriptor: int) -> Path:
    if sys.platform == "darwin":
        import fcntl

        return Path(os.fsdecode(fcntl.fcntl(descriptor, 1029, bytes(1024)).split(b"\0", 1)[0]))
    return Path(os.readlink(f"/proc/self/fd/{descriptor}"))


def _resolve(path: object, dir_fd: object, event: str) -> Path | None:
    candidate: Path | None = None
    try:
        if isinstance(path, int):
            candidate = _descriptor_path(path)
        elif isinstance(path, (str, bytes)):
            candidate = Path(os.fsdecode(path))
        else:
            return None
        if not candidate.is_absolute() and isinstance(dir_fd, int) and dir_fd >= 0:
            candidate = _descriptor_path(dir_fd) / candidate
        if event in {"os.remove", "os.rmdir", "os.rename", "os.link", "os.symlink"}:
            # These mutate the directory entry, not a symlink's destination.
            return candidate.parent.resolve() / candidate.name
        return candidate.resolve()
    except (OSError, ValueError):
        # Resolution failure must not erase an attempt at a known home path.
        return Path(os.path.abspath(candidate)) if candidate is not None else None


def _audit(event: str, args: tuple[object, ...]) -> None:
    if not _active:
        return
    for path, dir_fd in _mutations(event, args):
        resolved = _resolve(path, dir_fd, event)
        if resolved is None:
            continue
        for observer in _active:
            if not resolved.is_relative_to(observer.root):
                continue
            relative = resolved.relative_to(observer.root).as_posix()
            observer.writes.append(f"pid={os.getpid()} {event}: ~/.gobby/{relative}")
            if observer.log is not None:
                with observer.log.open("a") as stream:
                    stream.write(json.dumps(observer.writes[-1]) + "\n")


@contextmanager
def observe_writes(root: Path, log: Path | None = None) -> Iterator[WriteAudit]:
    """Record mutating audit events from this interpreter, never other processes.

    Audit hooks cannot be removed, so install one dispatcher and activate an
    observer only for the scope being checked. Paths are resolved to catch aliases.
    Records describe attempts: failed mutations must not hide a sandbox escape.
    """
    global _installed
    observer = WriteAudit(root.resolve(), log)
    if log is not None and log.resolve().is_relative_to(observer.root):
        raise ValueError("The audit log must be outside the monitored home")
    if not _installed:
        sys.addaudithook(_audit)
        _installed = True
    _active.append(observer)
    try:
        yield observer
    finally:
        _active.remove(observer)


@contextmanager
def daemon_write_audit() -> Iterator[None]:
    """Opt the isolated daemon into the parent's temporary attribution log."""
    root = os.environ.get(ROOT_ENV)
    log = os.environ.get(LOG_ENV)
    if root is None or log is None:
        yield
        return
    with observe_writes(Path(root), Path(log)):
        yield


def write_log_offset() -> int:
    log = os.environ.get(LOG_ENV)
    return Path(log).stat().st_size if log is not None else 0


def daemon_writes(offset: int = 0) -> list[str]:
    log = os.environ.get(LOG_ENV)
    if log is None:
        return []
    with Path(log).open("rb") as stream:
        stream.seek(offset)
        return [json.loads(line) for line in stream.readlines() if line.endswith(b"\n")]
