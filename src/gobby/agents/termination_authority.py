"""Who may stop or kill another seat's agent run.

One helper answers for the external termination tools: stop_agent, kill_agent, and
cancel_stale_helpers. Daemon-internal paths (lifecycle monitor, team teardown, enforcement)
do not pass through it.
"""

from __future__ import annotations

from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.agent_resolver import session_has_capability_or_operator

# Declared in a definition's ``capabilities`` list to stop or kill any seat's run.
STOP_ANY_SEAT = "stop_any_seat"

# Typed refusal code returned by stop_agent and kill_agent before any signal or cancel.
NOT_OWNED_ERROR = "agent_stop_not_owned"

# Spawner hops walked from a run upward. Matches the default agent depth limit of 5, so a
# legitimate chain always fits; a deeper chain is refused rather than walked further.
_MAX_SPAWNER_HOPS = 5


def caller_may_terminate(
    db: HubDatabase,
    *,
    caller_session_id: str | None,
    spawner_session_id: str,
    run_session_id: str | None,
) -> bool:
    """Whether ``caller_session_id`` may stop or kill a run.

    Admits, in order: a request with no caller session; the run's own child session; a
    caller on the spawner chain, starting at the run's recorded spawner (so ownership holds
    before the child session registers); and a caller whose definition declares
    ``STOP_ANY_SEAT`` or who is an unspawned operator terminal.

    A request with no caller session is not seat traffic. Wrapper requests without a
    session are refused upstream with SESSION_REQUIRED, so only operator-authenticated,
    non-wrapper requests reach here without one.
    """
    if caller_session_id is None:
        return True
    if run_session_id is not None and caller_session_id == run_session_id:
        return True
    if _on_spawner_chain(db, caller_session_id, spawner_session_id):
        return True
    return session_has_capability_or_operator(db, caller_session_id, STOP_ANY_SEAT)


def _on_spawner_chain(db: HubDatabase, caller_session_id: str, spawner_session_id: str) -> bool:
    """Whether the caller spawned the run, or spawned one of its spawner's spawners."""
    sessions = SessionManager(db)
    seen: set[str] = set()
    current: str | None = spawner_session_id
    for _ in range(_MAX_SPAWNER_HOPS):
        if current is None or current in seen:
            return False
        if current == caller_session_id:
            return True
        seen.add(current)
        session = sessions.get(current)
        current = session.parent_session_id if session is not None else None
    return False


def not_owned_refusal(run_id: str) -> dict[str, Any]:
    """The typed refusal both termination tools return for a run the caller does not own."""
    return _not_owned(f"Agent run {run_id}", run_id=run_id)


def helpers_not_owned_refusal(parent_session_id: str) -> dict[str, Any]:
    """Typed refusal from cancel_stale_helpers for a parent's helpers the caller does not own."""
    return _not_owned(
        f"Helper runs of parent session {parent_session_id}",
        parent_session_id=parent_session_id,
    )


def _not_owned(subject: str, **target: str) -> dict[str, Any]:
    return {
        "success": False,
        "error_code": NOT_OWNED_ERROR,
        **target,
        "error": (
            f"{subject} can only be stopped by its spawner chain "
            f"or a seat whose definition declares {STOP_ANY_SEAT}."
        ),
    }
