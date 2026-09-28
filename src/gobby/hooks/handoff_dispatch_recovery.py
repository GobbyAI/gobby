"""Settle a reclaimed handoff dispatch whose provider boundary already landed."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery import (
    _COMPACT_BOUNDARY_CONFIRM_SECONDS,
    _COMPACT_BOUNDARY_POLL_SECONDS,
    _COMPACT_BOUNDARY_TIMEOUT_REASON,
    _compact_receipt_exists,
)
from gobby.sessions.codex_compact_watch import continue_pending_handoff, release_awaiting_handoff
from gobby.sessions.compact_continuation import (
    arm_compact_boundary_waiter,
    consume_and_schedule_handoff_compact_continuation,
    mark_handoff_compact_continuation_pending,
    register_compact_boundary_waiter,
    unregister_compact_boundary_waiter,
)
from gobby.sessions.compact_markers import COMPACT_NOTIFICATION_STARTED_AT_VARIABLE
from gobby.sessions.handoff import ClaimedHandoffDelivery
from gobby.sessions.handoff_records import record_handoff_delivery
from gobby.sessions.transcript_cursor import (
    CodexRolloutCursor,
    TranscriptObservationError,
    codex_compact_boundary_between,
)
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


async def confirm_reclaimed_compact(
    db: HubDatabase,
    claimed: ClaimedHandoffDelivery,
    session: Any,
    started_at: datetime,
    *,
    event_loop: Any,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> dict[str, Any]:
    """Wait out a compact a dead dispatch already submitted, without typing it again.

    PreCompact fired in the dead process, so its boundary wait and Codex rollout
    watcher died with it. This takes both over until the dead dispatch's own
    deadline, then reports the same result a live dispatch would.
    """
    session_id = str(session.id)
    codex = getattr(session, "source", None) == "codex"
    waiter = register_compact_boundary_waiter(
        session_id,
        claimed.attempt_id,
        claimed.handoff_record_id,
        getattr(session, "terminal_context", None),
    )
    arm_compact_boundary_waiter(session_id, claimed.attempt_id)
    cursor: CodexRolloutCursor | None = None
    if codex:
        try:
            cursor = CodexRolloutCursor.at_eof(getattr(session, "transcript_path", None))
        except TranscriptObservationError:
            cursor = None
    deadline = started_at + timedelta(seconds=_COMPACT_BOUNDARY_CONFIRM_SECONDS)
    try:
        # The cursor starts now, so a boundary since the claim was checked is read in full.
        landed = await asyncio.to_thread(_compact_landed, db, session, started_at)
        while not landed and not waiter.event.is_set():
            remaining = (deadline - utc_now()).total_seconds()
            if remaining <= 0:
                break
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    waiter.event.wait(), timeout=min(_COMPACT_BOUNDARY_POLL_SECONDS, remaining)
                )
            if cursor is not None:
                try:
                    landed = await asyncio.to_thread(cursor.saw_fresh_compacted)
                except TranscriptObservationError:
                    cursor = None
            elif not codex:
                landed = await asyncio.to_thread(_compact_landed, db, session, started_at)
            # SessionStart(compact) receipts the handoff without a PostCompact notify.
            landed = landed or _compact_receipt_exists(
                db, claimed.handoff_record_id, claimed.attempt_id
            )
    finally:
        unregister_compact_boundary_waiter(session_id, claimed.attempt_id)
    if not landed and not _compact_receipt_exists(
        db, claimed.handoff_record_id, claimed.attempt_id
    ):
        # No SessionStart will move the row on, and awaiting_handoff declines every wake.
        await asyncio.to_thread(release_awaiting_handoff, db, session_id)
        return {
            "compacted": False,
            "reason": _COMPACT_BOUNDARY_TIMEOUT_REASON,
            "error_code": "compact_unconfirmed",
        }
    record_handoff_delivery(
        db,
        handoff_id=claimed.handoff_record_id,
        attempt_id=claimed.attempt_id,
        boundary_kind="compact",
        continuation_session_id=session_id,
    )
    if codex:
        # Codex fires no compact SessionStart: the pull prompt and release are ours.
        await asyncio.to_thread(
            continue_pending_handoff,
            db,
            session,
            loop=event_loop,
            terminal_manager=terminal_manager,
            terminal_runtime_registry=terminal_runtime_registry,
        )
        await asyncio.to_thread(release_awaiting_handoff, db, session_id)
    return {"compacted": True, "cli": session.source, "via": "reclaimed"}


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
