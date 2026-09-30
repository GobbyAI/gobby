"""The operator hold that keeps a parked seat's task claims through its absence.

A directed pause -- an operator updating a CLI, say -- exits the seat's process,
and the liveness monitor then expires the session under the live host epoch
exactly as it would a dead seat. Nothing in the row tells the two apart, so no
writer infers a hold from a CLI exit: a root terminal operator places one
explicitly on the seat it is parking.

Unlike the contested-expiry marker, which describes one expiry and is cleared by
the next status write, the hold outlives the seat's status writes (the expiry
it anticipates is one of them). Only an explicit release or a proven resume --
``revive_expired_terminal_session`` bringing the seat back to ``active`` --
clears it, and ``SESSION_REVIVAL_HORIZON_HOURS`` bounds it: a seat nobody
resumed within the horizon is dead, and its claims release on the ordinary
schedule. ``release_task_claim`` and live-session recovery both honor it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from gobby.sessions.contested_expiry import (
    CONTESTED_EXPIRY_STAMP_PATTERN,
    contested_expiry_stamp,
)
from gobby.utils.datetime import parse_stored_datetime, utc_now

OPERATOR_CLAIM_HOLD_VARIABLE = "operator_claim_hold"

# Same fixed-width UTC stamp as the contested-expiry marker, for the same
# reason: the SQL shield compares it as text, the Python shield as a datetime.
_STAMP_RE = re.compile(CONTESTED_EXPIRY_STAMP_PATTERN)


def operator_claim_hold_payload(
    actor_session_id: str, reason: str, recorded_at: datetime
) -> dict[str, str]:
    """Build the hold an operator places on a seat it is parking."""
    return {
        "actor_session_id": actor_session_id,
        "reason": reason,
        "created_at": contested_expiry_stamp(recorded_at),
    }


def operator_claim_hold_recorded_at(variables: Mapping[str, Any] | None) -> datetime | None:
    """Return when the seat's hold was placed, or None when there is no valid hold."""
    if not isinstance(variables, Mapping):
        return None
    payload = variables.get(OPERATOR_CLAIM_HOLD_VARIABLE)
    if not isinstance(payload, Mapping):
        return None
    recorded_at = payload.get("created_at")
    if not isinstance(recorded_at, str) or _STAMP_RE.fullmatch(recorded_at) is None:
        return None
    try:
        return parse_stored_datetime(recorded_at)
    except ValueError:
        return None


def operator_claim_hold_horizon() -> timedelta:
    """Return the hold's lifetime: the session revival horizon."""
    # Deferred: the storage package imports this module through its storage side.
    from gobby.storage.sessions._constants import SESSION_REVIVAL_HORIZON_HOURS

    return timedelta(hours=SESSION_REVIVAL_HORIZON_HOURS)


def is_operator_claim_held(variables: Mapping[str, Any] | None) -> bool:
    """Report whether a hold placed within the revival horizon covers this seat.

    A stamp in the future reads as no hold, as the SQL shield's text range does.
    """
    recorded_at = operator_claim_hold_recorded_at(variables)
    if recorded_at is None:
        return False
    now = utc_now()
    return now - operator_claim_hold_horizon() <= recorded_at <= now


__all__ = [
    "OPERATOR_CLAIM_HOLD_VARIABLE",
    "is_operator_claim_held",
    "operator_claim_hold_horizon",
    "operator_claim_hold_payload",
    "operator_claim_hold_recorded_at",
]
