"""Read-modify-write Claude configuration under its native mkdir lock."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_LOCK_STALE_SECONDS = 10.0
_LOCK_UPDATE_SECONDS = 5.0
_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_RETRY_SECONDS = 0.05


class ClaudeConfigError(OSError):
    """A config operation failed without falling back to an unlocked write."""


def claude_config_home() -> Path:
    """Match Claude's config home, including literal empty and tilde selectors."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return (
        Path(unicodedata.normalize("NFC", configured))
        if configured is not None
        else Path.home() / ".claude"
    )


def claude_config_path() -> Path:
    """Select the same legacy or current global config file as Claude."""
    legacy = claude_config_home() / ".config.json"
    if legacy.exists() or legacy.is_symlink():
        return legacy
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(configured) if configured else Path.home()
    suffix = "-custom-oauth" if os.environ.get("CLAUDE_CODE_CUSTOM_OAUTH_URL") else ""
    return root / f".claude{suffix}.json"


@contextmanager
def _config_lock(path: Path) -> Iterator[Callable[[], None]]:
    """Cooperate with proper-lockfile's 10s stale / 5s update defaults."""
    lock_path = path.with_name(f"{path.name}.lock")
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        if time.monotonic() >= deadline:
            raise ClaudeConfigError(f"Timed out waiting for Claude config lock: {lock_path}")
        try:
            lock_path.mkdir(mode=0o700)
            break
        except FileExistsError:
            try:
                existing = lock_path.lstat()
                if not stat.S_ISDIR(existing.st_mode):
                    raise ClaudeConfigError(f"Claude config lock is not a directory: {lock_path}")
                if existing.st_mtime < time.time() - _LOCK_STALE_SECONDS:
                    lock_path.rmdir()
                    continue
            except FileNotFoundError:
                continue
            time.sleep(_LOCK_RETRY_SECONDS)

    identity = lock_path.stat()
    mtime_ns = identity.st_mtime_ns
    state_lock = threading.Lock()
    stop = threading.Event()
    compromised: OSError | None = None

    def check_unlocked() -> None:
        current = lock_path.lstat()
        if (
            current.st_dev != identity.st_dev
            or current.st_ino != identity.st_ino
            or current.st_mtime_ns != mtime_ns
        ):
            raise ClaudeConfigError(f"Claude config lock ownership changed: {lock_path}")

    def check_owned() -> None:
        with state_lock:
            if compromised is not None:
                raise ClaudeConfigError(
                    f"Claude config lock was compromised: {lock_path}"
                ) from compromised
            check_unlocked()

    def heartbeat() -> None:
        nonlocal mtime_ns, compromised
        while not stop.wait(_LOCK_UPDATE_SECONDS):
            with state_lock:
                try:
                    check_unlocked()
                    os.utime(lock_path, None)
                    mtime_ns = lock_path.stat().st_mtime_ns
                except OSError as exc:
                    compromised = exc
                    return

    updater = threading.Thread(target=heartbeat, name="claude-config-lock", daemon=True)
    started = False
    try:
        updater.start()
        started = True
        yield check_owned
    finally:
        stop.set()
        if started:
            updater.join()
        try:
            current = lock_path.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                lock_path.rmdir()
        except FileNotFoundError:
            pass


def update_claude_config(path: Path, update: Callable[[dict[str, Any]], bool]) -> bool:
    """Apply an updater to a fresh locked read; write atomically only when changed.

    The updater returns whether to persist its changes. Exceptions or lock loss
    leave the original config intact. All callers, including async adapters,
    keep the complete read/modify/replace operation inside this boundary.
    """
    if path.is_symlink():
        raise ClaudeConfigError(f"Failed to read {path}: config must not be a symlink")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _config_lock(path) as check_owned:
        try:
            if path.exists():
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
            else:
                data = {}
        except json.JSONDecodeError as exc:
            raise ClaudeConfigError(f"Failed to parse {path}: {exc}") from exc
        except OSError as exc:
            raise ClaudeConfigError(f"Failed to read {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ClaudeConfigError(f"Failed to parse {path}: expected JSON object")
        if not update(data):
            return False
        check_owned()
        temp_path: str | None = None
        try:
            fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f"{path.stem}_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            check_owned()
            os.replace(temp_path, path)
        except OSError as exc:
            raise ClaudeConfigError(f"Failed to write {path}: {exc}") from exc
        finally:
            if temp_path is not None:
                Path(temp_path).unlink(missing_ok=True)
        return True
