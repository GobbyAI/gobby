"""The marker a speculative terminal expiry leaves behind, and how to read it.

Two SessionStart paths expire a terminal session before anything validates who
owns the terminal: the reused-context scan and the parent-registration branch.
Both run on a guess, and ``revive_expired_terminal_session`` routinely reverses
them, so an ``expired`` status written by either one can simply be wrong. Every
other expiry writer -- inactivity, a killed tmux server, an explicit close --
knows the session is finished.

The row itself cannot tell those apart, so the speculative writer records why it
expired the session and the claim shields read that cause back. Only the two
causes below are contestable; an expiry that left no marker is final and its
claims release on the ordinary schedule (#20837).

The marker lives in ``session_variables`` beside the compact-continue marker,
which encodes the same kind of fact -- an owner whose status is transiently
stale -- and is read by the same ``release_task_claim`` statement.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, get_args

from gobby.utils.datetime import parse_stored_datetime

CONTESTED_TERMINAL_EXPIRY_VARIABLE = "contested_terminal_expiry"

# context_reuse: a newly registering session's terminal context matched this
# one's, so the reused-context scan expired it.
# parent_registration: a child session registered under this one as its parent.
ContestedExpiryCause = Literal["context_reuse", "parent_registration"]

# session_variables is a shared store, so a fresh created_at under this key is
# not on its own evidence of a contest. Only a cause naming one of the two
# speculative writers is, and both shields check the name against this set.
CONTESTED_EXPIRY_CAUSES: frozenset[str] = frozenset(get_args(ContestedExpiryCause))


def contested_expiry_payload(cause: ContestedExpiryCause, recorded_at: datetime) -> dict[str, str]:
    """Build the marker a speculative expiry stores on the session it expired."""
    return {"cause": cause, "created_at": recorded_at.isoformat()}


def contested_expiry_recorded_at(variables: Mapping[str, Any] | None) -> datetime | None:
    """Return when a speculative expiry marked this session, if one did.

    Returns None for a session with no marker, which is every session expired
    by a writer that knew the session was finished, and for a marker whose cause
    names neither speculative writer.
    """
    if not isinstance(variables, Mapping):
        return None
    payload = variables.get(CONTESTED_TERMINAL_EXPIRY_VARIABLE)
    if not isinstance(payload, Mapping):
        return None
    cause = payload.get("cause")
    if not isinstance(cause, str) or cause not in CONTESTED_EXPIRY_CAUSES:
        return None
    recorded_at = payload.get("created_at")
    if not isinstance(recorded_at, str):
        return None
    try:
        return parse_stored_datetime(recorded_at)
    except ValueError:
        # A marker that cannot be read is no marker: claim recovery has to keep
        # running past a corrupt variables row, not raise out of the sweep.
        return None


__all__ = [
    "CONTESTED_EXPIRY_CAUSES",
    "CONTESTED_TERMINAL_EXPIRY_VARIABLE",
    "ContestedExpiryCause",
    "contested_expiry_payload",
    "contested_expiry_recorded_at",
]
