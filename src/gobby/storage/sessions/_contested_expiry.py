"""Persist and read back the marker a speculative terminal expiry leaves.

The marker's meaning lives in ``gobby.sessions.contested_expiry``; this module
is only its storage side. It merges into the same ``session_variables`` row the
workflow state manager writes, so it takes the same per-session lock rather
than racing a concurrent variable write.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from gobby.sessions.contested_expiry import (
    CONTESTED_TERMINAL_EXPIRY_VARIABLE,
    ContestedExpiryCause,
    contested_expiry_payload,
)
from gobby.storage.hub.protocol import HubDatabase, SessionVariableMutation
from gobby.utils.datetime import utc_now


def record_contested_terminal_expiry(
    db: HubDatabase,
    session_id: str,
    cause: ContestedExpiryCause,
) -> None:
    """Record that this terminal session's expiry was a guess about ownership."""
    now = utc_now()
    payload = contested_expiry_payload(cause, now)
    stamp = now.isoformat()
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO session_variables (session_id, variables, updated_at) "
                "VALUES (%s, %s, %s)",
                (session_id, json.dumps({CONTESTED_TERMINAL_EXPIRY_VARIABLE: payload}), stamp),
            )
            return
        variables = _stored_variables(row)
        variables[CONTESTED_TERMINAL_EXPIRY_VARIABLE] = payload
        conn.execute(
            "UPDATE session_variables SET variables = %s, updated_at = %s WHERE session_id = %s",
            (json.dumps(variables), stamp, session_id),
        )


def read_session_variables(db: HubDatabase, session_id: str) -> dict[str, Any] | None:
    """Return a session's stored variables, or None when it has no row."""
    row = db.fetchone(
        "SELECT variables FROM session_variables WHERE session_id = %s",
        (session_id,),
    )
    if row is None:
        return None
    return _stored_variables(row)


def _stored_variables(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    raw = row["variables"] if isinstance(row, Mapping) else row[0]
    if isinstance(raw, str):
        raw = json.loads(raw)
    return dict(raw) if isinstance(raw, Mapping) else {}


__all__ = ["read_session_variables", "record_contested_terminal_expiry"]
