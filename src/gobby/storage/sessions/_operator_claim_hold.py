"""Persist and clear the operator hold on a parked seat's task claims.

The hold's meaning lives in ``gobby.sessions.operator_claim_hold``; this module
is only its storage side. It writes the same ``session_variables`` row as the
workflow state manager, under the same per-session lock.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from gobby.sessions.contested_expiry import CONTESTED_EXPIRY_STAMP_PATTERN, contested_expiry_stamp
from gobby.sessions.operator_claim_hold import (
    OPERATOR_CLAIM_HOLD_VARIABLE,
    live_operator_claim_hold_actor,
    operator_claim_hold_payload,
)
from gobby.storage.hub.protocol import HubDatabase, SessionVariableMutation
from gobby.utils.datetime import utc_now

from ._constants import SESSION_REVIVAL_HORIZON_HOURS
from ._contested_expiry import _stored_variables

# Conditions over a `session_variables sv` row. Casting an arbitrary jsonb
# string to timestamptz raises out of a sweep, so a marker's stamp is compared
# as text: the fixed-width pattern makes that comparison chronological and
# pg_input_is_valid refuses calendar-invalid values (Feb 30), which together
# admit exactly the stamps the Python shields parse. The bounds are the revival
# horizon and now, so a future stamp reads as no marker.
VALID_STAMP_SQL = """sv.variables -> %s ->> 'created_at' ~ %s
           AND pg_input_is_valid(sv.variables -> %s ->> 'created_at', 'timestamptz')
           AND sv.variables -> %s ->> 'created_at' >= %s
           AND sv.variables -> %s ->> 'created_at' <= %s"""

# The attestation keys must be nonempty JSON strings, matching the Python
# shield: ->> alone would also accept a number or an object.
LIVE_OPERATOR_CLAIM_HOLD_SQL = f"""jsonb_typeof(sv.variables -> %s) = 'object'
           AND jsonb_typeof(sv.variables -> %s -> 'actor_session_id') = 'string'
           AND sv.variables -> %s ->> 'actor_session_id' <> ''
           AND jsonb_typeof(sv.variables -> %s -> 'reason') = 'string'
           AND sv.variables -> %s ->> 'reason' <> ''
           AND {VALID_STAMP_SQL}"""


def valid_stamp_params(variable: str, now: datetime) -> list[Any]:
    """Parameters for VALID_STAMP_SQL on `variable`, in placeholder order."""
    earliest = contested_expiry_stamp(now - timedelta(hours=SESSION_REVIVAL_HORIZON_HOURS))
    latest = contested_expiry_stamp(now)
    return [
        variable,
        CONTESTED_EXPIRY_STAMP_PATTERN,
        variable,
        variable,
        earliest,
        variable,
        latest,
    ]


def live_operator_claim_hold_params(now: datetime) -> list[Any]:
    """Parameters for LIVE_OPERATOR_CLAIM_HOLD_SQL, in placeholder order."""
    key = OPERATOR_CLAIM_HOLD_VARIABLE
    return [key, key, key, key, key, *valid_stamp_params(key, now)]


def record_operator_claim_hold(
    db: HubDatabase,
    session_id: str,
    *,
    actor_session_id: str,
    reason: str,
) -> datetime:
    """Place (or renew) the hold on `session_id` and return when it was placed.

    Raises OperatorClaimHoldHeldByOther when another session holds it live.
    """
    now = utc_now()
    payload = {
        OPERATOR_CLAIM_HOLD_VARIABLE: operator_claim_hold_payload(actor_session_id, reason, now)
    }
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        _refuse_foreign_holder(conn, session_id, actor_session_id)
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


class OperatorClaimHoldHeldByOther(Exception):
    """Another session placed the live hold; only it may renew or release it."""

    def __init__(self, actor_session_id: str) -> None:
        super().__init__(f"claim hold is held by session {actor_session_id}")
        self.actor_session_id = actor_session_id


def _refuse_foreign_holder(conn: Any, session_id: str, actor_session_id: str) -> None:
    row = conn.execute(
        "SELECT variables FROM session_variables WHERE session_id = %s",
        (session_id,),
    ).fetchone()
    holder = live_operator_claim_hold_actor(_stored_variables(row) if row is not None else None)
    if holder is not None and holder != actor_session_id:
        raise OperatorClaimHoldHeldByOther(holder)


def release_operator_claim_hold(db: HubDatabase, session_id: str, *, actor_session_id: str) -> bool:
    """Release the hold as its issuer; report whether one existed.

    Raises OperatorClaimHoldHeldByOther when another session holds it live. A
    lapsed or malformed hold no longer protects anything, so anyone may clear it.
    """
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        _refuse_foreign_holder(conn, session_id, actor_session_id)
        return _delete_hold(conn, session_id)


def clear_operator_claim_hold(db: HubDatabase, session_id: str) -> bool:
    """Drop the hold on a proven resume; report whether one existed."""
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        return _delete_hold(conn, session_id)


def _delete_hold(conn: Any, session_id: str) -> bool:
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
    return bool(cursor.rowcount == 1)


__all__ = [
    "LIVE_OPERATOR_CLAIM_HOLD_SQL",
    "VALID_STAMP_SQL",
    "OperatorClaimHoldHeldByOther",
    "clear_operator_claim_hold",
    "live_operator_claim_hold_params",
    "record_operator_claim_hold",
    "release_operator_claim_hold",
    "valid_stamp_params",
]
