"""Settle a reclaimed handoff dispatch whose provider boundary already landed."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from gobby.sessions.compact_continuation import (
    consume_and_schedule_handoff_compact_continuation,
    mark_handoff_compact_continuation_pending,
)
from gobby.sessions.compact_markers import COMPACT_NOTIFICATION_STARTED_AT_VARIABLE
from gobby.sessions.handoff import ClaimedHandoffDelivery
from gobby.sessions.handoff_records import record_handoff_delivery
from gobby.sessions.transcript_cursor import codex_compact_boundary_between
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now
from gobby.workflows.state_manager import SessionVariableManager


def settle_landed_boundary(
    db: HubDatabase,
    claimed: ClaimedHandoffDelivery,
    session: Any,
    started_at: datetime,
    *,
    event_loop: Any,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> bool:
    """Return True when the dead dispatch already crossed its boundary.

    Resubmitting then would compact or clear twice, so a landed compact is
    receipted and continued instead; a landed clear leaves its successor pull.
    """
    kind = "clear" if claimed.clear_session else "compact"
    receipt = db.fetchone(
        "SELECT 1 FROM session_handoff_deliveries "
        "WHERE handoff_id = %s AND attempt_id = %s AND boundary_kind = %s",
        (claimed.handoff_record_id, claimed.attempt_id, kind),
    )
    if claimed.clear_session:
        return isinstance(receipt, Mapping)
    if not isinstance(receipt, Mapping) and not _compact_landed(db, session, started_at):
        return False
    record_handoff_delivery(
        db,
        handoff_id=claimed.handoff_record_id,
        attempt_id=claimed.attempt_id,
        boundary_kind="compact",
        continuation_session_id=claimed.session_id,
    )
    mark_handoff_compact_continuation_pending(db, claimed.session_id, attempt_id=claimed.attempt_id)
    consume_and_schedule_handoff_compact_continuation(
        db,
        pending_session_id=claimed.session_id,
        target_session=session,
        loop=event_loop,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=terminal_runtime_registry,
    )
    return True


def _compact_landed(db: HubDatabase, session: Any, started_at: datetime) -> bool:
    if getattr(session, "source", None) == "codex":
        return (
            codex_compact_boundary_between(
                getattr(session, "transcript_path", None),
                str(getattr(session, "external_id", "")),
                started_at,
                utc_now(),
            )
            is not None
        )
    raw = (
        SessionVariableManager(db)
        .get_variables(str(session.id))
        .get(COMPACT_NOTIFICATION_STARTED_AT_VARIABLE)
    )
    try:
        boundary = datetime.fromisoformat(raw) if isinstance(raw, str) and raw else None
    except ValueError:
        return False
    return boundary is not None and boundary.tzinfo is not None and boundary >= started_at
