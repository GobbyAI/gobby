"""Private, bounded disk checkpoints for incremental transcript evidence."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

from gobby.paths import get_gobby_home

logger = logging.getLogger(__name__)

_DISK_SNAPSHOT_LIMIT = 64
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_MAX_CACHE_BYTES = 256 * 1024 * 1024


def _cache_dir() -> Path:
    return get_gobby_home() / "cache" / "transcript-evidence-v1"


def _snapshot_path(session_id: str) -> Path:
    key = hashlib.sha256(session_id.encode()).hexdigest()
    return _cache_dir() / f"{key}.json"


def clear_snapshots() -> None:
    for path in _cache_dir().glob("*.json"):
        path.unlink(missing_ok=True)


def read_snapshot(session_id: str) -> bytes | None:
    try:
        path = _snapshot_path(session_id)
        if path.stat().st_size > _MAX_SNAPSHOT_BYTES:
            return None
        with path.open("rb") as stream:
            payload = stream.read(_MAX_SNAPSHOT_BYTES + 1)
        return payload if len(payload) <= _MAX_SNAPSHOT_BYTES else None
    except OSError:
        return None


def write_snapshot(session_id: str, payload: bytes) -> None:
    """Replace a checkpoint atomically; cache failure must not fail a hook."""
    if len(payload) > _MAX_SNAPSHOT_BYTES:
        return
    path = _snapshot_path(session_id)
    temporary: str | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, path)
        temporary = None
        entries = sorted(
            (item.stat().st_mtime_ns, item.stat().st_size, item)
            for item in path.parent.glob("*.json")
        )
        total_bytes = sum(size for _, size, _ in entries)
        for index, (_, size, stale) in enumerate(entries):
            if len(entries) - index <= _DISK_SNAPSHOT_LIMIT and total_bytes <= _MAX_CACHE_BYTES:
                break
            stale.unlink(missing_ok=True)
            total_bytes -= size
    except OSError:
        logger.debug("Could not persist transcript evidence checkpoint", exc_info=True)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
