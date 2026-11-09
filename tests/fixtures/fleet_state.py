"""Healthy fleet state for tests, kept fresh for as long as a test holds it."""

from __future__ import annotations

import json
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


def write_healthy_fleet_state(path: Path, *, sampled_at: float) -> bytes:
    """Write a healthy state file and return the exact bytes written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(healthy_fleet_document(sampled_at)).encode("utf-8")
    path.write_bytes(payload)
    return payload


@contextmanager
def keep_fleet_state_fresh(
    path: Path,
    *,
    interval_seconds: float = FLEET_STATE_REFRESH_SECONDS,
    clock: Callable[[], float] | None = None,
) -> Iterator[None]:
    """Write a healthy state now and re-stamp it every interval while the block runs."""
    # Red stage: nothing is written here and nothing refreshes the state.
    yield


__all__ = [
    "FLEET_STATE_REFRESH_SECONDS",
    "healthy_fleet_document",
    "keep_fleet_state_fresh",
    "write_healthy_fleet_state",
]
