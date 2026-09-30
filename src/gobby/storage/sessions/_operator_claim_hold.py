"""Persist and clear the operator hold on a parked seat's task claims.

The hold's meaning lives in ``gobby.sessions.operator_claim_hold``; this module
is only its storage side. It writes the same ``session_variables`` row as the
workflow state manager, under the same per-session lock.
"""

from __future__ import annotations

import json
from datetime import datetime

from gobby.sessions.operator_claim_hold import (
    OPERATOR_CLAIM_HOLD_VARIABLE,
    operator_claim_hold_payload,
)
from gobby.storage.hub.protocol import HubDatabase, SessionVariableMutation
from gobby.utils.datetime import utc_now


def record_operator_claim_hold(
    db: HubDatabase,
    session_id: str,
    *,
    actor_session_id: str,
    reason: str,
) -> datetime:
    """Place (or renew) the hold on `session_id` and return when it was placed."""
    now = utc_now()
    payload = {
        OPERATOR_CLAIM_HOLD_VARIABLE: operator_claim_hold_payload(actor_session_id, reason, now)
    }
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        conn.execute(
            """
            INSERT INTO session_variables (session_id, variables, updated_at)
            VALUES (%s, %s::jsonb, %s)
            ON CONFLICT (session_id) DO UPDATE
               SET variables = COALESCE(session_variables.variables, '{}'::jsonb)
                               || EXCLUDED.variables,
                   updated_at = EXCLUDED.updated_at
            """,
            (session_id, json.dumps(payload), now),
        )
    return now


def clear_operator_claim_hold(db: HubDatabase, session_id: str) -> bool:
    """Drop the hold on an explicit release or a proven resume; report whether one existed."""
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        cursor = conn.execute(
            """
            UPDATE session_variables
               SET variables = variables - %s,
                   updated_at = %s
             WHERE session_id = %s
               AND jsonb_exists(variables, %s)
            """,
            (OPERATOR_CLAIM_HOLD_VARIABLE, utc_now(), session_id, OPERATOR_CLAIM_HOLD_VARIABLE),
        )
    return cursor.rowcount == 1


__all__ = ["clear_operator_claim_hold", "record_operator_claim_hold"]
