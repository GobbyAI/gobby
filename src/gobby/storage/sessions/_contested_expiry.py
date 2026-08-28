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


def clear_contested_terminal_expiry(db: HubDatabase, session_id: str) -> None:
    """Drop the marker once the contest it describes has been settled.

    A speculative expiry is in doubt only until the session's status is written
    again. Leaving the marker behind would let a session contested once and
    revived shield a later, genuinely final expiry for the rest of the revival
    horizon, so every status write past the speculative one clears it.
    """
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        conn.execute(
            """
            UPDATE session_variables
               SET variables = variables - %s,
                   updated_at = %s
             WHERE session_id = %s
               AND jsonb_exists(variables, %s)
            """,
            (
                CONTESTED_TERMINAL_EXPIRY_VARIABLE,
                utc_now().isoformat(),
                session_id,
                CONTESTED_TERMINAL_EXPIRY_VARIABLE,
            ),
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


def session_has_active_native_subagent(db: HubDatabase, session_id: str) -> bool:
    """Return whether a native Claude/Codex/Grok subagent is in flight on this session."""
    variables = read_session_variables(db, session_id)
    if not variables:
        return False
    raw_count = variables.get("subagent_count") or 0
    try:
        count = int(raw_count)
    except (TypeError, ValueError):
        count = 0
    return count > 0 or bool(variables.get("is_subagent"))


def _stored_variables(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    raw = row["variables"] if isinstance(row, Mapping) else row[0]
    if isinstance(raw, str):
        raw = json.loads(raw)
    return dict(raw) if isinstance(raw, Mapping) else {}


__all__ = [
    "session_has_active_native_subagent",
    "clear_contested_terminal_expiry",
    "read_session_variables",
    "record_contested_terminal_expiry",
]
