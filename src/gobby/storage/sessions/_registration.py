"""Session registration helpers."""

from __future__ import annotations

from datetime import datetime

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager, Machine, MachineNotRegisteredError


def require_registered_machine(db: HubDatabase, machine_id: str, *, seen_at: datetime) -> Machine:
    """Refresh last-seen and fail if the machine was never enrolled."""
    machine = LocalMachineManager(db).refresh_seen(machine_id, seen_at=seen_at)
    if machine is None:
        raise MachineNotRegisteredError(
            f"Machine {machine_id} is not registered; run authenticated enrollment first"
        )
    return machine
