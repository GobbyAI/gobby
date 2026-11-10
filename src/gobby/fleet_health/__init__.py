"""Fleet health admission: refuse new seats and launches while the daemon is unhealthy.

The watcher (``gdaemon fleet-watch``) writes the state file; this package only reads it.
"""

from gobby.fleet_health.gate import (
    FLEET_UNHEALTHY,
    FleetAdmissionRefusal,
    launch_refusal,
    spawn_refusal,
)
from gobby.fleet_health.state import FleetState, fleet_state_path, read_fleet_state

__all__ = [
    "FLEET_UNHEALTHY",
    "FleetAdmissionRefusal",
    "FleetState",
    "fleet_state_path",
    "launch_refusal",
    "read_fleet_state",
    "spawn_refusal",
]
