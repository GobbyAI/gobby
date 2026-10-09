"""Persist and read back the marker a speculative terminal expiry leaves.

The marker's meaning lives in ``gobby.sessions.contested_expiry``; this module
is only its storage side. It merges into the same ``session_variables`` row the
workflow state manager writes, so it takes the same per-session lock rather
than racing a concurrent variable write.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from typing import Any

from gobby.sessions.contested_expiry import (
    CONTESTED_TERMINAL_EXPIRY_VARIABLE,
    ContestedExpiryCause,
    contested_expiry_payload,
)
from gobby.storage.hub.protocol import HubDatabase, SessionVariableMutation
from gobby.storage.hub.read_scope import SESSION_VARIABLES_TABLES, scoped_read
from gobby.utils.datetime import utc_now

_VARIABLES_ROW_TABLES = SESSION_VARIABLES_TABLES | {"sessions"}


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


def read_session_variables_row(
    db: HubDatabase, session_id: str, *, keys: Collection[str] | None = None
) -> Mapping[str, Any]:
    """Return ``stored``, ``variables`` and the session's ``project_id`` in one row.

    Explicit key scopes project stored values in PostgreSQL and cache separately
    from full reads. Project metadata remains available for readers that layer defaults.
    """
    key_scope = None if keys is None else tuple(sorted(set(keys)))
    variables = "sv.variables::text"
    params: tuple[Any, ...] = (session_id, session_id)
    cache_key: tuple[Any, ...] = ("session_variables", session_id)
    if key_scope is not None:
        variables = (
            "(SELECT COALESCE(jsonb_object_agg(key, sv.variables -> key), '{}'::jsonb)"
            " FROM unnest(%s::text[]) AS requested(key)"
            " WHERE jsonb_typeof(sv.variables) = 'object' AND sv.variables ? key)::text"
        )
        params = (list(key_scope), session_id, session_id)
        cache_key = (*cache_key, key_scope)
    row = scoped_read(
        db,
        cache_key,
        _VARIABLES_ROW_TABLES,
        lambda: db.fetchone(
            f"SELECT sv.session_id IS NOT NULL AS stored, {variables} AS variables,"
            " s.project_id"
            " FROM (SELECT 1) AS one"
            " LEFT JOIN session_variables sv ON sv.session_id = %s"
            " LEFT JOIN sessions s ON s.id = %s",
            params,
        ),
    )
    return row or {"stored": False, "variables": None, "project_id": None}


def read_session_variables(
    db: HubDatabase, session_id: str, *, keys: Collection[str] | None = None
) -> dict[str, Any] | None:
    """Return a session's stored variables, or None when it has no row."""
    row = read_session_variables_row(db, session_id, keys=keys)
    if not row["stored"]:
        return None
    return _stored_variables(row)


def session_has_active_native_subagent(db: HubDatabase, session_id: str) -> bool:
    """Return whether a native Claude/Codex/Grok subagent is in flight on this session."""
    variables = read_session_variables(db, session_id, keys=("subagent_count", "is_subagent"))
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
    "read_session_variables_row",
    "record_contested_terminal_expiry",
]
