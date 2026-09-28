"""Continue a Codex compact that no handoff dispatch is watching.

Codex sends no PostCompact and defers SessionStart(compact) to the next prompt,
so an operator-typed ``/compact`` would otherwise leave the session in
``awaiting_handoff``, where every wake is declined and no prompt ever arrives.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from gobby.sessions.compact_continuation import (
    _schedule_coroutine,
    compact_boundary_wait_submitted,
    consume_and_schedule_handoff_compact_continuation,
    mark_handoff_compact_continuation_pending,
)
from gobby.sessions.compact_markers import HANDOFF_COMPACT_CONTINUE_VARIABLE
from gobby.sessions.handoff import HANDOFF_DISPATCH_GATE_VARIABLE, PENDING_HANDOFF_VARIABLE
from gobby.sessions.transcript_cursor import CodexRolloutCursor, TranscriptObservationError
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager

CODEX_COMPACT_WATCH_SECONDS = 600.0
CODEX_COMPACT_WATCH_POLL_SECONDS = 2.0


def watch_codex_out_of_band_compact(
    db: HubDatabase,
    session: Any,
    *,
    loop: Any | None,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> bool:
    """Watch the rollout for the boundary unless a handoff dispatch already does."""
    if compact_boundary_wait_submitted(str(session.id)):
        return False
    try:
        cursor = CodexRolloutCursor.at_eof(getattr(session, "transcript_path", None))
    except TranscriptObservationError:
        return False
    return _schedule_coroutine(
        _watch(
            db,
            session,
            cursor,
            loop=loop,
            terminal_manager=terminal_manager,
            terminal_runtime_registry=terminal_runtime_registry,
        ),
        loop=loop,
    )


async def _watch(
    db: HubDatabase,
    session: Any,
    cursor: CodexRolloutCursor,
    *,
    loop: Any | None,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> None:
    deadline = asyncio.get_running_loop().time() + CODEX_COMPACT_WATCH_SECONDS
    compacted = False
    while not compacted and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(CODEX_COMPACT_WATCH_POLL_SECONDS)
        try:
            compacted = await asyncio.to_thread(cursor.saw_fresh_compacted)
        except TranscriptObservationError:
            break
    session_id = str(session.id)
    if compact_boundary_wait_submitted(session_id):
        return
    if compacted:
        await asyncio.to_thread(
            continue_pending_handoff,
            db,
            session,
            loop=loop,
            terminal_manager=terminal_manager,
            terminal_runtime_registry=terminal_runtime_registry,
        )
    await asyncio.to_thread(release_awaiting_handoff, db, session_id)


def continue_pending_handoff(
    db: HubDatabase,
    session: Any,
    *,
    loop: Any | None,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> bool:
    """Send the pull prompt whose SessionStart(compact) lets get_handoff deliver."""
    session_id = str(session.id)
    variables = SessionVariableManager(db).get_variables(session_id)
    continuation = variables.get(HANDOFF_COMPACT_CONTINUE_VARIABLE)
    marker = variables.get(PENDING_HANDOFF_VARIABLE)
    gate = variables.get(HANDOFF_DISPATCH_GATE_VARIABLE)
    if isinstance(continuation, Mapping):
        attempt_id = continuation.get("attempt_id")
    elif isinstance(marker, Mapping) and marker.get("clear_session") is False:
        attempt_id = marker.get("attempt_id")
    elif isinstance(gate, Mapping) and gate.get("error_code") == "compact_unconfirmed":
        attempt_id = gate.get("attempt_id")
    else:
        return False
    # A continuation marked by an earlier timed-out dispatch is past its freshness window.
    mark_handoff_compact_continuation_pending(
        db, session_id, attempt_id=attempt_id if isinstance(attempt_id, str) else None
    )
    return consume_and_schedule_handoff_compact_continuation(
        db,
        pending_session_id=session_id,
        target_session=session,
        loop=loop,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=terminal_runtime_registry,
    )


def release_awaiting_handoff(db: HubDatabase, session_id: str) -> None:
    manager = SessionManager(db)
    current = manager.get(session_id)
    if current is not None and current.status == "awaiting_handoff":
        manager.update_session_status(session_id, "paused")
