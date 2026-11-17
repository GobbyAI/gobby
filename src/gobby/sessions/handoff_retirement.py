"""Retire operational clear-handoff state without changing historical records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from gobby.sessions.compact_markers import (
    COMPACT_HANDOFF_MARKER_VARIABLE,
    COMPACT_NOTIFICATION_STARTED_AT_VARIABLE,
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

# Frozen persisted keys: retirement does not depend on the live handoff protocol.
CLEAR_ATTEMPT_VARIABLE = "clear_attempt"
_PENDING = "set_handoff_pending"
_GATE = "context_compact_handoff_result"
_PULL = "handoff_pull_pending"
_TURN_END = "_handoff_turn_end_pending"
_FOUND_WORK = "set_handoff_found_work"
_FAILED = "failed_handoff_attempt"


def retire_legacy_clear_handoff(db: HubDatabase, session_id: str) -> bool:
    """Disarm obsolete clear state while retaining authored records and receipts."""
    with db.transaction() as conn:
        conn.execute("SELECT id FROM sessions WHERE id = %s FOR UPDATE", (session_id,))
        row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if row is None:
            return False
        raw = row["variables"]
        variables: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        pending = variables.get(_PENDING)
        clear_pending = isinstance(pending, Mapping) and pending.get("clear_session") is True
        clear_marker = variables.pop("clear_attempt", None)
        gate = variables.get(_GATE)
        clear_gate = isinstance(gate, Mapping) and gate.get("clear_session") is True
        orphan_pull = not isinstance(pending, Mapping) and (
            variables.get(_PULL) is True or variables.get(_TURN_END) is True
        )
        if not clear_pending and clear_marker is None and not clear_gate and not orphan_pull:
            return False
        if clear_gate:
            variables.pop(_GATE, None)
        if orphan_pull:
            variables.pop(_PULL, None)
            variables.pop(_TURN_END, None)
        if isinstance(pending, Mapping) and clear_pending:
            variables[_FAILED] = {
                "attempt_id": pending.get("attempt_id"),
                "handoff_record_id": pending.get("handoff_record_id"),
                "delivery_state": "failed_not_deliverable",
                "found_work": variables.get(_FOUND_WORK),
            }
            for key in (_PENDING, _GATE, _PULL, _TURN_END):
                variables.pop(key, None)
            prior_status = pending.get("prior_status")
            conn.execute(
                "UPDATE sessions SET status = %s, updated_at = %s "
                "WHERE id = %s AND status = 'awaiting_handoff'",
                (
                    prior_status
                    if prior_status in {"active", "idle", "paused", "waiting"}
                    else "active",
                    utc_now(),
                    session_id,
                ),
            )
        conn.execute(
            "UPDATE session_variables SET variables = %s, updated_at = %s WHERE session_id = %s",
            (json.dumps(variables), utc_now(), session_id),
        )
        return True


def retire_session_handoff(db: HubDatabase, session_id: str) -> None:
    """A definitive fresh boundary supersedes all pending delivery on the old row."""
    with db.transaction() as conn:
        conn.execute("SELECT id FROM sessions WHERE id = %s FOR UPDATE", (session_id,))
        row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if row is None:
            return
        raw = row["variables"]
        variables: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        pending = variables.get(_PENDING)
        if isinstance(pending, Mapping):
            variables[_FAILED] = {
                "attempt_id": pending.get("attempt_id"),
                "handoff_record_id": pending.get("handoff_record_id"),
                "delivery_state": "failed_not_deliverable",
                "found_work": variables.get(_FOUND_WORK),
            }
        for key in (
            _PENDING,
            _GATE,
            _PULL,
            _TURN_END,
            "clear_attempt",
            COMPACT_HANDOFF_MARKER_VARIABLE,
            COMPACT_NOTIFICATION_STARTED_AT_VARIABLE,
            HANDOFF_COMPACT_CONTINUE_VARIABLE,
        ):
            variables.pop(key, None)
        conn.execute(
            "UPDATE session_variables SET variables = %s, updated_at = %s WHERE session_id = %s",
            (json.dumps(variables), utc_now(), session_id),
        )


def retire_legacy_clear_handoffs(db: HubDatabase, machine_id: str) -> None:
    """Disarm every obsolete attempt on this machine before recovery can replay it."""
    rows = db.fetchall(
        """
        SELECT s.id FROM sessions s JOIN session_variables v ON v.session_id = s.id
        WHERE s.machine_id = %s AND (
            v.variables -> 'set_handoff_pending' ->> 'clear_session' = 'true'
            OR v.variables -> 'context_compact_handoff_result' ->> 'clear_session' = 'true'
            OR v.variables ? 'clear_attempt'
            OR ((v.variables ->> 'handoff_pull_pending' = 'true'
                 OR v.variables ->> '_handoff_turn_end_pending' = 'true')
                AND jsonb_typeof(v.variables -> 'set_handoff_pending') IS DISTINCT FROM 'object')
        )
        """,
        (machine_id,),
    )
    for row in rows:
        retire_legacy_clear_handoff(db, str(row["id"]))
