"""Atomic continuation marker storage and delivered-readiness recovery."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from gobby.sessions.compact_markers import HANDOFF_COMPACT_CONTINUE_VARIABLE
from gobby.sessions.handoff import HANDOFF_DISPATCH_GATE_VARIABLE, HANDOFF_TURN_END_PENDING_VARIABLE
from gobby.storage.hub.protocol import HubDatabase, SessionVariableMutation

logger = logging.getLogger(__name__)


def _fail_delivered_readiness(db: HubDatabase, session_id: str, attempt_id: str) -> None:
    """Release tools for a delivered attempt without permitting another compact."""
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s", (session_id,)
        ).fetchone()
        variables = _load_variables(_row_variables(row))
        pending = variables.get(HANDOFF_COMPACT_CONTINUE_VARIABLE)
        gate = variables.get(HANDOFF_DISPATCH_GATE_VARIABLE)
        if (
            not isinstance(pending, dict)
            or pending.get("attempt_id") != attempt_id
            or not isinstance(gate, dict)
            or gate.get("attempt_id") != attempt_id
        ):
            return
        receipt = conn.execute(
            "SELECT 1 FROM session_handoff_deliveries WHERE attempt_id = %s "
            "AND boundary_kind = 'compact'",
            (attempt_id,),
        ).fetchone()
        if receipt is None:
            return
        variables.pop(HANDOFF_TURN_END_PENDING_VARIABLE, None)
        gate.update(
            delivery_failed=True,
            delivery_pending=False,
            attempt_pending=False,
            error_code="compact_unconfirmed",
            readiness_unconfirmed=True,
            reason="Codex compact readiness timed out after delivery",
            retry_guidance="Call get_handoff; do not submit another compact.",
        )
        conn.execute(
            "UPDATE session_variables SET variables = %s, updated_at = %s WHERE session_id = %s",
            (json.dumps(variables), datetime.now(UTC).isoformat(), session_id),
        )


def _merge_session_variable(
    db: HubDatabase,
    session_id: str,
    name: str,
    value: Any,
) -> None:
    now = datetime.now(UTC).isoformat()
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        variables = _load_variables(_row_variables(row))
        variables[name] = value
        if row:
            conn.execute(
                "UPDATE session_variables SET variables = %s, updated_at = %s WHERE session_id = %s",
                (json.dumps(variables), now, session_id),
            )
        else:
            conn.execute(
                "INSERT INTO session_variables (session_id, variables, updated_at) "
                "VALUES (%s, %s, %s)",
                (session_id, json.dumps(variables), now),
            )


def _restore_session_variable_if_absent(
    db: HubDatabase,
    session_id: str,
    name: str,
    value: Any,
) -> bool:
    """Restore a consumed value without replacing a concurrently written value."""
    now = datetime.now(UTC).isoformat()
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        variables = _load_variables(_row_variables(row))
        if name in variables:
            return False
        variables[name] = value
        if row:
            conn.execute(
                "UPDATE session_variables SET variables = %s, updated_at = %s WHERE session_id = %s",
                (json.dumps(variables), now, session_id),
            )
        else:
            conn.execute(
                "INSERT INTO session_variables (session_id, variables, updated_at) "
                "VALUES (%s, %s, %s)",
                (session_id, json.dumps(variables), now),
            )
        return True


def _load_session_variables(db: HubDatabase, session_id: str) -> dict[str, Any]:
    row = db.fetchone(
        "SELECT variables FROM session_variables WHERE session_id = %s",
        (session_id,),
    )
    return _load_variables(_row_variables(row))


def _pop_session_variable(
    db: HubDatabase,
    session_id: str,
    name: str,
    *,
    expected_attempt_id: str | None = None,
) -> Any:
    now = datetime.now(UTC).isoformat()
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        variables = _load_variables(_row_variables(row))
        current_value = variables.get(name)
        if expected_attempt_id is not None and (
            not isinstance(current_value, dict)
            or current_value.get("attempt_id") != expected_attempt_id
        ):
            return None
        value = variables.pop(name, None)
        if value is not None:
            conn.execute(
                "UPDATE session_variables SET variables = %s, updated_at = %s WHERE session_id = %s",
                (json.dumps(variables), now, session_id),
            )
        return value


def _row_variables(row: Any) -> Any:
    if row is None:
        return None
    try:
        return row["variables"]
    except Exception:
        return None


def _load_variables(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, bytes):
        raw = raw.decode()
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        preview = raw[:80].replace("\n", "\\n")
        logger.warning(
            "Corrupt set_handoff compact continuation variables JSON ignored: %s; preview=%r",
            exc,
            preview,
        )
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
