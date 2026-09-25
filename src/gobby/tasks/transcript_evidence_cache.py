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
        return _snapshot_path(session_id).read_bytes()
    except OSError:
        return None


def write_snapshot(session_id: str, payload: bytes) -> None:
    """Replace a checkpoint atomically; cache failure must not fail a hook."""
    path = _snapshot_path(session_id)
    temporary: str | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, path)
        temporary = None
        entries = sorted(path.parent.glob("*.json"), key=lambda item: item.stat().st_mtime_ns)
        for stale in entries[:-_DISK_SNAPSHOT_LIMIT]:
            stale.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not persist transcript evidence checkpoint", exc_info=True)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
