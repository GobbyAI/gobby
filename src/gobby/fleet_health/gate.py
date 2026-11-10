"""Admission for new seats and launches: refuse while the fleet is not healthy.

While unhealthy, every admission is refused with ``fleet_unhealthy`` and the watcher's reasons.
A healthy fleet admits every seat and launch; pacing new work is the coordinator's job.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from gobby.fleet_health.state import fleet_state_path, read_fleet_state

FLEET_UNHEALTHY = "fleet_unhealthy"

_HEALTHY = "healthy"


@dataclass(frozen=True)
class FleetAdmissionRefusal:
    """A typed refusal to admit new work, with the fleet state that caused it."""

    code: str
    message: str
    state: str
    reasons: tuple[str, ...] = ()

    def as_result(self) -> dict[str, Any]:
        """The refusal in the shape spawn results already use."""
        return {
            "success": False,
            "error": self.message,
            "error_code": self.code,
            "fleet": {"state": self.state, "reasons": list(self.reasons)},
        }


def launch_refusal(now: float | None = None) -> FleetAdmissionRefusal | None:
    """Refuse a launch while the fleet is unhealthy."""
    return _admit(time.time() if now is None else now)


def spawn_refusal(now: float | None = None) -> FleetAdmissionRefusal | None:
    """Refuse one new seat while the fleet is unhealthy."""
    return _admit(time.time() if now is None else now)


def _admit(now: float) -> FleetAdmissionRefusal | None:
    fleet = read_fleet_state(fleet_state_path(), now)
    if fleet.state == _HEALTHY:
        return None
    return FleetAdmissionRefusal(
        FLEET_UNHEALTHY,
        f"fleet_unhealthy: daemon health is {fleet.state}; new seats are refused "
        f"until it is healthy ({'; '.join(fleet.reasons)})",
        fleet.state,
        fleet.reasons,
    )
