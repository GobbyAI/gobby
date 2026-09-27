"""Reconcile a compact boundary confirmed after handoff timeout compensation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from gobby.sessions.compact_markers import COMPACT_NOTIFICATION_STARTED_AT_VARIABLE
from gobby.sessions.handoff import (
    FAILED_HANDOFF_VARIABLE,
    HANDOFF_DELIVERY_FAILURES_VARIABLE,
    HANDOFF_DISPATCH_GATE_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    ConsumedHandoff,
    _found_work_from_marker,
    _load_variables,
    _store_variables,
)
from gobby.sessions.handoff_records import insert_delivery_receipt
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.found_work_gate import arm_found_work_gate


def reconcile_late_compact_handoff(
    db: HubDatabase, session_id: str, attempt_id: str
) -> tuple[ConsumedHandoff, bool] | None:
    """Deliver only the caller's failed attempt with a timely provider compact marker."""
    with db.transaction() as conn:
        session = conn.execute(
            "SELECT status FROM sessions WHERE id = %s FOR UPDATE", (session_id,)
        ).fetchone()
        if session is None or session["status"] != "active":
            return None
        variable_row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if variable_row is None:
            return None
        variables = _load_variables(variable_row["variables"])
        marker = variables.get(FAILED_HANDOFF_VARIABLE)
        gate = variables.get(HANDOFF_DISPATCH_GATE_VARIABLE)
        if (
            not isinstance(marker, Mapping)
            or marker.get("attempt_id") != attempt_id
            or marker.get("delivery_state") != "failed_not_deliverable"
            or not isinstance(gate, Mapping)
            or gate.get("attempt_id") != attempt_id
            or gate.get("delivery_failed") is not True
            or gate.get("clear_session") is not False
            or gate.get("error_code")
            not in {"interrupt_unconfirmed", "compact_unconfirmed", "compact_failed"}
            or PENDING_HANDOFF_VARIABLE in variables
        ):
            return None
        handoff_id = marker.get("handoff_record_id")
        if not isinstance(handoff_id, str):
            return None
        handoff = conn.execute(
            """
            SELECT h.rendered_markdown, h.authored_at FROM session_handoffs AS h
            WHERE h.id = %s AND h.session_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM session_handoff_deliveries AS d WHERE d.handoff_id = h.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM session_handoffs AS later
                  WHERE later.session_id = h.session_id AND later.authored_at > h.authored_at
              )
            FOR UPDATE OF h
            """,
            (handoff_id, session_id),
        ).fetchone()
        if handoff is None:
            return None
        raw_boundary = variables.get(COMPACT_NOTIFICATION_STARTED_AT_VARIABLE)
        if not isinstance(raw_boundary, str):
            return None
        try:
            boundary = datetime.fromisoformat(raw_boundary)
        except (TypeError, ValueError):
            return None
        authored = handoff["authored_at"]
        freshness = (
            timedelta(minutes=20)
            if gate.get("error_code") == "compact_unconfirmed"
            else timedelta(minutes=10)
        )
        if (
            boundary.tzinfo is None
            or authored.tzinfo is None
            or not authored <= boundary <= authored + freshness
        ):
            return None
        insert_delivery_receipt(
            conn,
            handoff_id=handoff_id,
            attempt_id=attempt_id,
            boundary_kind="compact",
            continuation_session_id=session_id,
        )
        found_work = _found_work_from_marker(marker.get("found_work"))
        consumed = ConsumedHandoff(
            session_id, handoff_id, attempt_id, str(handoff["rendered_markdown"]), found_work
        )
        gate_armed = bool(consumed.open_found_work) or "found_work" not in marker
        if gate_armed:
            arm_found_work_gate(variables)
        variables.pop(FAILED_HANDOFF_VARIABLE, None)
        variables.pop(HANDOFF_DISPATCH_GATE_VARIABLE, None)
        variables[HANDOFF_DELIVERY_FAILURES_VARIABLE] = 0
        _store_variables(conn, session_id, variables, exists=True)
        return consumed, gate_armed
