"""Admission for new seats and launches: refuse while the fleet is not healthy or is ramping.

While unhealthy, every admission is refused with ``fleet_unhealthy`` and the watcher's reasons.
While healthy, at most ``max_new_seats`` seats are admitted per ``ramp_window_seconds``. The
window never starts before the last non-healthy sample. There is no bypass.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from gobby.fleet_health.ledger import ledger_lock, ledger_path, read_admissions, write_admissions
from gobby.fleet_health.state import fleet_state_path, read_fleet_state

FLEET_UNHEALTHY = "fleet_unhealthy"
FLEET_RAMP_CAPPED = "fleet_ramp_capped"
FLEET_LEDGER_UNREADABLE = "fleet_ledger_unreadable"

_HEALTHY = "healthy"
_ADMISSION_LOCK = threading.Lock()


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


def launch_refusal(now: float | None = None, *, ramp: bool = True) -> FleetAdmissionRefusal | None:
    """Refuse a launch while the fleet is unhealthy or its ramp has no room for one seat.

    A launch starts no seat itself, so nothing is recorded. Each seat it starts passes
    through ``spawn_refusal`` and is recorded there. A nested run passes ``ramp=False``:
    it checks health only, because its parent's room check already passed and each of
    its seats is paced at its own spawn.
    """
    with _ADMISSION_LOCK:
        return _admit(time.time() if now is None else now, record=False, ramp=ramp)


def spawn_refusal(now: float | None = None) -> FleetAdmissionRefusal | None:
    """Refuse one new seat, or admit it and record the admission in the ledger."""
    with _ADMISSION_LOCK:
        return _admit(time.time() if now is None else now, record=True)


def _admit(now: float, *, record: bool, ramp: bool = True) -> FleetAdmissionRefusal | None:
    fleet = read_fleet_state(fleet_state_path(), now)
    if fleet.state != _HEALTHY:
        return FleetAdmissionRefusal(
            FLEET_UNHEALTHY,
            f"fleet_unhealthy: daemon health is {fleet.state}; new seats are refused "
            f"until it is healthy ({'; '.join(fleet.reasons)})",
            fleet.state,
            fleet.reasons,
        )

    if not ramp:
        return None

    path = ledger_path()
    # Locking, reading and writing the ledger all fail the same way: a refusal, never a raise.
    try:
        with ledger_lock(path):
            admissions = read_admissions(path)
            if admissions is None:
                return FleetAdmissionRefusal(
                    FLEET_LEDGER_UNREADABLE,
                    f"fleet_ledger_unreadable: {path} cannot be read, so the ramp cannot be verified",
                    fleet.state,
                )

            # Every admission in the trailing window counts, including those made before a
            # non-healthy sample: a flap must not reset the cap.
            window = float(fleet.ramp_window_seconds)
            recent = [at for at in admissions if at > now - window]
            if len(recent) >= fleet.max_new_seats:
                return FleetAdmissionRefusal(
                    FLEET_RAMP_CAPPED,
                    f"fleet_ramp_capped: {len(recent)} of {fleet.max_new_seats} new seats already "
                    f"admitted in the last {fleet.ramp_window_seconds}s; admission resumes as the "
                    "ramp window rolls",
                    fleet.state,
                )

            if record:
                write_admissions(path, [*recent, now])
    except OSError as error:
        return FleetAdmissionRefusal(
            FLEET_LEDGER_UNREADABLE,
            f"fleet_ledger_unreadable: {path} cannot be locked or written: {error}",
            fleet.state,
        )
    return None
