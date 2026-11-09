"""Healthy fleet state for tests, kept fresh for as long as a test holds it."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# The fleet-watch cadence. The gate treats a state file older than 90 seconds as stale.
FLEET_STATE_REFRESH_SECONDS = 30


def healthy_fleet_document(sampled_at: float) -> dict[str, Any]:
    """Return a healthy state with the ramp wide open, as the root fixture has always written."""
    return {
        "schema_version": 1,
        "state": "healthy",
        "sampled_at_unix": sampled_at,
        "last_non_healthy_at_unix": None,
        "reasons": [],
        "ramp": {"max_new_seats": 1000, "ramp_window_seconds": 900},
        "thresholds_source": "defaults",
        "watcher_pid": 0,
    }


def _publish(path: Path, payload: bytes) -> None:
    """Replace the state file in one step, so a concurrent reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as file:
        staged = Path(file.name)
        try:
            file.write(payload)
            file.close()
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)


@contextmanager
def _fleet_state_lock(path: Path) -> Iterator[None]:
    """Hold the one lock every writer of the state file takes, so no write lands mid-check.

    The lock is a sibling file, because publishing replaces the state file's inode.
    flock conflicts across open file descriptions, so two threads of one test conflict too.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(f"{path.name}.lock").open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def publish_fleet_document(path: Path, document: dict[str, Any]) -> bytes:
    """Publish a state document under the lock and return the exact bytes written."""
    payload = json.dumps(document).encode("utf-8")
    with _fleet_state_lock(path):
        _publish(path, payload)
    return payload


def remove_fleet_state(path: Path) -> None:
    """Remove the state file under the writers' lock, so no refresh can put it back."""
    with _fleet_state_lock(path):
        path.unlink(missing_ok=True)


def write_healthy_fleet_state(path: Path, *, sampled_at: float) -> bytes:
    """Write a healthy state file and return the exact bytes written."""
    return publish_fleet_document(path, healthy_fleet_document(sampled_at))


def refresh_healthy_fleet_state(path: Path, *, last: bytes, sampled_at: float) -> bytes | None:
    """Re-stamp the state while it still holds the bytes last written; None means another writer owns it.

    The check and the replace run under the lock, so a negative write cannot land between them.
    """
    with _fleet_state_lock(path):
        if path.read_bytes() != last:
            return None
        payload = json.dumps(healthy_fleet_document(sampled_at)).encode("utf-8")
        _publish(path, payload)
        return payload


@contextmanager
def keep_fleet_state_fresh(
    path: Path,
    *,
    interval_seconds: float = FLEET_STATE_REFRESH_SECONDS,
    clock: Callable[[], float] | None = None,
) -> Iterator[None]:
    """Write a healthy state now and re-stamp it every interval while the block runs.

    The refresher stops as soon as another writer changes the file, so a test that
    writes its own condition keeps that condition.
    """
    now = clock if clock is not None else time.time
    last = write_healthy_fleet_state(path, sampled_at=now())
    stop = threading.Event()

    def refresh() -> None:
        nonlocal last
        while not stop.wait(interval_seconds):
            try:
                renewed = refresh_healthy_fleet_state(path, last=last, sampled_at=now())
            except OSError:
                return  # The next read reports the file as stale, the visible failure.
            if renewed is None:
                return  # Another writer owns the file now; leave its state alone.
            last = renewed

    worker = threading.Thread(target=refresh, name="fleet-state-refresh", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=5)


__all__ = [
    "FLEET_STATE_REFRESH_SECONDS",
    "healthy_fleet_document",
    "keep_fleet_state_fresh",
    "write_healthy_fleet_state",
]
