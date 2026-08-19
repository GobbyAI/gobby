"""Session registration helpers."""

from __future__ import annotations

from datetime import datetime

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager, Machine, MachineNotRegisteredError

from ._title_defaults import MANUAL_TITLE_SOURCE
from ._update_sentinel import UNSET, UnsetType, is_set


def manual_registration_title(
    title: str | None | UnsetType,
    title_source: str | None | UnsetType,
) -> tuple[str | UnsetType, str | UnsetType]:
    """Classify a non-empty registration title as user-owned."""
    if not is_set(title) or not isinstance(title, str) or not title.strip():
        return UNSET, UNSET
    if is_set(title_source) and title_source not in {None, MANUAL_TITLE_SOURCE}:
        return UNSET, UNSET
    return title.strip(), MANUAL_TITLE_SOURCE


def require_registered_machine(db: HubDatabase, machine_id: str, *, seen_at: datetime) -> Machine:
    """Refresh last-seen and fail if the machine was never enrolled."""
    machine = LocalMachineManager(db).refresh_seen(machine_id, seen_at=seen_at)
    if machine is None:
        raise MachineNotRegisteredError(
            f"Machine {machine_id} is not registered; run authenticated enrollment first"
        )
    return machine


def require_valid_title_source(
    title_source: str | None | UnsetType,
    valid_sources: set[str],
) -> None:
    if is_set(title_source) and title_source is not None and title_source not in valid_sources:
        sources = ", ".join(sorted(valid_sources))
        raise ValueError(f"Invalid title_source {title_source!r}. Must be one of: {sources}")
