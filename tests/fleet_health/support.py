"""Write fleet state files for admission tests in the same shape the watcher writes."""

from __future__ import annotations

import json
import time

from gobby.fleet_health.state import fleet_state_path


def write_fleet_state(
    *,
    at: float | None = None,
    state: str = "healthy",
    reasons: tuple[str, ...] = (),
    last_non_healthy: float | None = None,
    max_new_seats: int = 3,
    ramp_window_seconds: int = 900,
) -> None:
    """Write the state file under the test Gobby home, sampled at ``at`` (default: now)."""
    path = fleet_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": 1,
        "state": state,
        "sampled_at_unix": time.time() if at is None else at,
        "last_non_healthy_at_unix": last_non_healthy,
        "reasons": list(reasons),
        "ramp": {
            "max_new_seats": max_new_seats,
            "ramp_window_seconds": ramp_window_seconds,
        },
        "thresholds_source": "defaults",
        "watcher_pid": 1,
    }
    path.write_text(json.dumps(document), encoding="utf-8")
