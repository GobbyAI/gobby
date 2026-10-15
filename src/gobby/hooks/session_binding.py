"""Machine scope for binding hook events to persisted session rows.

A hook from the hub's own machine (or one naming no machine) binds any row, as
before hub mode. A hook from another machine binds only that machine's rows, so
a node can never claim, mutate, or take its project from another machine's
session by naming its id.
"""

from __future__ import annotations

from typing import Protocol

from gobby.sessions.machine_scope import is_local_machine_owner
from gobby.utils.machine_id import get_machine_id


class _MachineOwned(Protocol):
    @property
    def machine_id(self) -> str | None: ...


def is_hub_machine_event(event_machine_id: str | None) -> bool:
    """Return whether an event came from this hub's own machine."""
    return not event_machine_id or event_machine_id == get_machine_id()


def session_binds_event(row_machine_id: str | None, event_machine_id: str | None) -> bool:
    """Return whether an event may bind a session row."""
    return is_hub_machine_event(event_machine_id) or is_local_machine_owner(
        row_machine_id, event_machine_id
    )


def bound_session[S: _MachineOwned](session: S | None, event_machine_id: str | None) -> S | None:
    """Return a matched row, or ``None`` when the event may not bind it."""
    if session is None or not session_binds_event(session.machine_id, event_machine_id):
        return None
    return session
