"""Post-result delivery for staged terminal compact handoffs."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.hooks.grok_pending_context import clear_queued_context
from gobby.mcp_proxy.tools.sessions._terminal import (
    _INTERRUPT_OBSERVATION_UNAVAILABLE_ERROR_CODE,
    _interrupt_observer,
    _resolve_pane_io,
    _send_terminal_compaction_command,
)
from gobby.mcp_proxy.tools.sessions._terminal_tmux import (
    _CLI_COMPACT_COMMANDS,
    composer_reader,
)
from gobby.sessions.compact_continuation import (
    CODEX_COMPACT_READY_CAPTURE_LINES,
    clear_handoff_compact_continuation_pending,
    mark_handoff_compact_continuation_pending,
    schedule_codex_handoff_compact_continuation_readiness,
)
from gobby.sessions.handoff import build_handoff_continue_prompt
from gobby.sessions.handoff_records import record_handoff_delivery
from gobby.sessions.transcript_cursor import (
    TranscriptObservationError,
    TurnSettledObserver,
    build_turn_settled_observer,
)

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


def _turn_settled_observer(source: str | None, session: Any) -> TurnSettledObserver | None:
    """Turn-state observer for CLIs that record turn boundaries; ``None`` interrupts first."""
    session_id = getattr(session, "id", None)
    try:
        return build_turn_settled_observer(
            source, getattr(session, "transcript_path", None), session_id=session_id
        )
    except TranscriptObservationError as exc:
        logger.warning(
            "Cannot observe %s turn state for handoff on session %s: %s", source, session_id, exc
        )
        return None


async def deliver_staged_compact_handoff(
    session_id: str,
    attempt_id: str,
    handoff_record_id: str,
    *,
    session_manager: SessionManager,
    db: HubDatabase,
    agent_run_manager: LocalAgentRunManager,
    terminal_manager: Any | None = None,
    terminal_runtime_registry: Any | None = None,
) -> dict[str, Any]:
    """Deliver one already-staged compact handoff after its MCP result completed."""
    session = session_manager.get(session_id)
    if session is None:
        return {"compacted": False, "reason": f"Session {session_id} not found"}
    source = getattr(session, "source", None)
    command = _CLI_COMPACT_COMMANDS.get(source) if source else None
    if command is None:
        return {"compacted": False, "reason": f"no compaction command known for cli={source!r}"}
    pane, error = _resolve_pane_io(
        session_id,
        session_manager,
        agent_run_manager,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=terminal_runtime_registry,
    )
    if error:
        return {"compacted": False, "reason": error}
    assert pane is not None
    observe_interrupt, observer_error = _interrupt_observer(source, session)
    if observer_error is not None:
        return {
            "compacted": False,
            "reason": observer_error,
            "error_code": _INTERRUPT_OBSERVATION_UNAVAILABLE_ERROR_CODE,
        }
    turn_settled = _turn_settled_observer(source, session)

    schedule_readiness: Callable[[str | None], bool] | None = None
    if source == "codex":

        def schedule_readiness(before_command: str | None) -> bool:
            return schedule_codex_handoff_compact_continuation_readiness(
                db,
                pane=pane,
                pending_session_id=session_id,
                before_command=before_command,
                attempt_id=attempt_id,
            )

    try:
        ok, reason, continuation_pending, detail = await _send_terminal_compaction_command(
            pane,
            command,
            session_id,
            cli_source=source,
            mark_continuation_pending=lambda: mark_handoff_compact_continuation_pending(
                db,
                session_id,
                prompt=build_handoff_continue_prompt(),
                attempt_id=attempt_id,
            ),
            clear_continuation_pending=lambda: clear_handoff_compact_continuation_pending(
                db,
                session_id,
                attempt_id=attempt_id,
            ),
            schedule_continuation_readiness=schedule_readiness,
            continuation_readiness_capture_lines=(
                CODEX_COMPACT_READY_CAPTURE_LINES if source == "codex" else None
            ),
            observe_interrupt=observe_interrupt,
            turn_settled=turn_settled,
            composer_read=composer_reader(db, source),
        )
    except Exception as exc:
        logger.warning(
            "Failed delivering compact handoff for session %s", session_id, exc_info=True
        )
        return {"compacted": False, "reason": str(exc), "error_code": "dispatch_failed"}
    if not ok:
        result: dict[str, Any] = {"compacted": False, "reason": reason}
        if detail is not None:
            result.update(detail)
        return result

    clear_queued_context(session_manager, session_id)
    try:
        delivered = record_handoff_delivery(
            db,
            handoff_id=handoff_record_id,
            attempt_id=attempt_id,
            boundary_kind="compact",
            continuation_session_id=session_id,
        )
    except Exception:
        logger.warning(
            "Failed recording compact handoff delivery %s for session %s",
            attempt_id,
            session_id,
            exc_info=True,
        )
        delivered = False
    return {
        "compacted": True,
        "command": command,
        "cli": source,
        "via": pane.backend,
        "interrupted": detail is None or detail.get("interrupted") is not False,
        "continuation_pending": continuation_pending,
        "attempt_id": attempt_id,
        "handoff_staged": True,
        "handoff_delivered": delivered,
    }
