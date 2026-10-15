"""Machine scope for binding hook events to persisted session rows.

Every hook binds only its own machine's rows, the hub's included: a node can
never claim, mutate, or take its project from another machine's session by
naming its id, and a local hook never binds a node's row. An event or row with
no machine binds nothing.
"""

from __future__ import annotations

from typing import Protocol

from gobby.sessions.machine_scope import is_local_machine_owner


class _MachineOwned(Protocol):
    @property
    def machine_id(self) -> str | None: ...


def session_binds_event(row_machine_id: str | None, event_machine_id: str | None) -> bool:
    """Return whether an event may bind a session row: both name the same machine."""
    return is_local_machine_owner(row_machine_id, event_machine_id)


def bound_session[S: _MachineOwned](session: S | None, event_machine_id: str | None) -> S | None:
    """Return a matched row, or ``None`` when the event may not bind it."""
    if session is None or not session_binds_event(session.machine_id, event_machine_id):
        return None
    return session
