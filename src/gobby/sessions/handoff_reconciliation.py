"""Reconcile a compact boundary confirmed after handoff timeout compensation, or settle
the attempt once its reconcile window closes without one."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from gobby.sessions.compact_markers import (
    COMPACT_NOTIFICATION_STARTED_AT_VARIABLE,
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
)
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
from gobby.sessions.transcript_cursor import codex_compact_boundary_between
from gobby.storage.hub.protocol import HubDatabase, SessionVariableMutation
from gobby.utils.datetime import utc_now
from gobby.workflows.found_work_gate import arm_found_work_gate

UNCONFIRMED_COMPACT_WINDOW = timedelta(minutes=20)
"""How long after authoring a provider boundary can still deliver an unconfirmed compact."""
_LATE_BOUNDARY_WINDOW = timedelta(minutes=10)


def _within(boundary: datetime | None, authored: datetime, window: timedelta) -> bool:
    return (
        boundary is not None
        and boundary.tzinfo is not None
        and authored.tzinfo is not None
        and authored <= boundary <= authored + window
    )


def _late_boundary(
    variables: Mapping[str, object],
    session: Mapping[str, Any],
    authored: datetime,
    window: timedelta,
    *,
    notification: bool = True,
) -> datetime | None:
    """Return the provider compact boundary that landed within ``window`` of ``authored``."""
    boundary = None
    raw_boundary = variables.get(COMPACT_NOTIFICATION_STARTED_AT_VARIABLE) if notification else None
    if isinstance(raw_boundary, str):
        try:
            boundary = datetime.fromisoformat(raw_boundary)
        except ValueError:
            boundary = None
    if not _within(boundary, authored, window) and session["source"] == "codex":
        boundary = codex_compact_boundary_between(
            session["transcript_path"],
            str(session["external_id"]),
            authored,
            authored + window,
        )
    return boundary if _within(boundary, authored, window) else None


def is_legacy_codex_ambiguous_enter_failure(gate: Mapping[str, object], source: str | None) -> bool:
    """Recognize failed Codex Enter writes recorded before they had an error code."""
    reason = gate.get("reason")
    if not isinstance(reason, str):
        return False
    pane_reason, separator, action = reason.partition(" (session ")
    return (
        source == "codex"
        and gate.get("error_code") is None
        and gate.get("clear_session") is False
        and bool(separator)
        and " key write failed (" in pane_reason
        and pane_reason.endswith(": enter")
        and action.endswith(" while submitting /compact)")
    )


def reconcile_late_compact_handoff(
    db: HubDatabase, session_id: str, attempt_id: str
) -> tuple[ConsumedHandoff, bool] | None:
    """Deliver only the caller's failed attempt with a timely provider compact marker."""
    with db.transaction() as conn:
        session = conn.execute(
            "SELECT status, source, transcript_path, external_id FROM sessions "
            "WHERE id = %s FOR UPDATE",
            (session_id,),
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
        legacy_ambiguous_enter = isinstance(
            gate, Mapping
        ) and is_legacy_codex_ambiguous_enter_failure(gate, session["source"])
        if (
            not isinstance(marker, Mapping)
            or marker.get("attempt_id") != attempt_id
            or marker.get("delivery_state") != "failed_not_deliverable"
            or not isinstance(gate, Mapping)
            or gate.get("attempt_id") != attempt_id
            or gate.get("delivery_failed") is not True
            or gate.get("clear_session") is not False
            or (
                gate.get("error_code")
                not in {"interrupt_unconfirmed", "compact_unconfirmed", "compact_failed"}
                and not legacy_ambiguous_enter
            )
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
        window = (
            UNCONFIRMED_COMPACT_WINDOW
            if gate.get("error_code") == "compact_unconfirmed"
            else _LATE_BOUNDARY_WINDOW
        )
        if (
            _late_boundary(
                variables,
                session,
                handoff["authored_at"],
                window,
                notification=not legacy_ambiguous_enter,
            )
            is None
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
        _release_attempt_gate(variables, attempt_id)
        variables[HANDOFF_DELIVERY_FAILURES_VARIABLE] = 0
        _store_variables(conn, session_id, variables, exists=True)
        return consumed, gate_armed


def _release_attempt_gate(variables: dict[str, Any], attempt_id: str) -> None:
    variables.pop(HANDOFF_DISPATCH_GATE_VARIABLE, None)
    continuation = variables.get(HANDOFF_COMPACT_CONTINUE_VARIABLE)
    if isinstance(continuation, Mapping) and continuation.get("attempt_id") == attempt_id:
        variables.pop(HANDOFF_COMPACT_CONTINUE_VARIABLE, None)


def settle_expired_unconfirmed_compact(
    db: HubDatabase, session_id: str, attempt_id: str
) -> datetime | bool:
    """Release an unconfirmed compact attempt's retry gate once its reconcile window closes.

    Returns the window's end while it is open, ``True`` when this call released the
    gate, and ``False`` when nothing is left to release: the attempt was delivered,
    superseded or already settled, its session ended, a newer attempt is in flight,
    or a boundary inside the window leaves it to ``reconcile_late_compact_handoff``.
    The failed marker stays, so ``get_handoff(failed_attempt_id=...)`` still reads the
    payload; nothing is delivered, receipted or typed.
    """
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        session = conn.execute(
            "SELECT status, source, transcript_path, external_id FROM sessions "
            "WHERE id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if session is None or session["status"] in ("expired", "deleted"):
            return False
        variable_row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if variable_row is None:
            return False
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
            or gate.get("error_code") != "compact_unconfirmed"
            or PENDING_HANDOFF_VARIABLE in variables
        ):
            return False
        handoff = conn.execute(
            """
            SELECT h.authored_at FROM session_handoffs AS h
            WHERE h.id = %s AND h.session_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM session_handoff_deliveries AS d WHERE d.handoff_id = h.id
              )
            FOR UPDATE OF h
            """,
            (marker.get("handoff_record_id"), session_id),
        ).fetchone()
        if handoff is None or handoff["authored_at"].tzinfo is None:
            return False
        authored: datetime = handoff["authored_at"]
        window_end = authored + UNCONFIRMED_COMPACT_WINDOW
        if utc_now() < window_end:
            return window_end
        if _late_boundary(variables, session, authored, UNCONFIRMED_COMPACT_WINDOW) is not None:
            return False
        _release_attempt_gate(variables, attempt_id)
        _store_variables(conn, session_id, variables, exists=True)
        return True
