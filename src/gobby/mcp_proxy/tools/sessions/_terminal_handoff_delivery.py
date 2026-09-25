"""Post-result delivery for staged terminal compact handoffs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from gobby.hooks.grok_pending_context import clear_queued_context
from gobby.mcp_proxy.tools.sessions._terminal import (
    _INTERRUPT_OBSERVATION_UNAVAILABLE_ERROR_CODE,
    _interrupt_observer,
    _resolve_pane_io,
    _send_terminal_compaction_command,
)
from gobby.mcp_proxy.tools.sessions._terminal_compaction import (
    _CLI_COMPACT_COMMANDS,
    _fresh_output_delta,
    composer_reader,
)
from gobby.sessions.compact_continuation import (
    CODEX_COMPACT_READY_CAPTURE_LINES,
    CompactBoundaryWaiter,
    arm_compact_boundary_waiter,
    clear_handoff_compact_continuation_pending,
    mark_handoff_compact_continuation_pending,
    register_compact_boundary_waiter,
    schedule_codex_handoff_compact_continuation_readiness,
    unregister_compact_boundary_waiter,
)
from gobby.sessions.handoff import build_handoff_continue_prompt
from gobby.sessions.transcript_cursor import (
    TranscriptObservationError,
    TranscriptTailCursor,
    TurnSettledObserver,
    build_turn_settled_observer,
)

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

_COMPACT_BOUNDARY_CONFIRM_SECONDS = 150.0
_COMPACT_BOUNDARY_POLL_SECONDS = 2.0
_COMPACT_PROVIDER_RETRIES = 2
_COMPACT_RETRY_BACKOFF_SECONDS = 1.0
_COMPACT_ERROR_PREFIX = "Error during compaction:"


def _claude_compact_error_cursor(session: Any) -> TranscriptTailCursor | None:
    if getattr(session, "source", None) != "claude":
        return None
    try:
        return TranscriptTailCursor.at_eof(getattr(session, "transcript_path", None))
    except TranscriptObservationError:
        return None


def _fresh_compact_error(cursor: TranscriptTailCursor | None) -> str | None:
    if cursor is None:
        return None
    try:
        for record in cursor.fresh_records():
            if record.get("type") != "system" or record.get("subtype") != "local_command":
                continue
            content = record.get("content")
            if not isinstance(content, str):
                continue
            stderr = content.removeprefix("<local-command-stderr>")
            if stderr.startswith(_COMPACT_ERROR_PREFIX):
                return stderr.split("</local-command-stderr>", 1)[0]
    except TranscriptObservationError:
        logger.debug("Compact error transcript observation ended", exc_info=True)
    return None


def _compact_receipt_exists(db: HubDatabase, handoff_record_id: str, attempt_id: str) -> bool:
    row = db.fetchone(
        "SELECT 1 FROM session_handoff_deliveries "
        "WHERE handoff_id = %s AND attempt_id = %s AND boundary_kind = 'compact'",
        (handoff_record_id, attempt_id),
    )
    return isinstance(row, Mapping)


async def _wait_for_compact_boundary(
    waiter: CompactBoundaryWaiter,
    pane: Any,
    before_command: str | None,
    cursor: TranscriptTailCursor | None,
) -> str | None:
    """Return an error or wait for one boundary within this delivery operation."""
    deadline = asyncio.get_running_loop().time() + _COMPACT_BOUNDARY_CONFIRM_SECONDS
    while not waiter.event.is_set():
        error = await asyncio.to_thread(_fresh_compact_error, cursor) if cursor else None
        if error is not None:
            return error
        try:
            after_command = await pane.snapshot(CODEX_COMPACT_READY_CAPTURE_LINES)
        except Exception:
            after_command = None
        if isinstance(before_command, str) and isinstance(after_command, str):
            fresh_output = _fresh_output_delta(before_command, after_command)
            for line in fresh_output.splitlines():
                if line.strip().startswith(_COMPACT_ERROR_PREFIX):
                    return line.strip()
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return "compact boundary was not observed before the confirmation deadline"
        try:
            await asyncio.wait_for(
                waiter.event.wait(), timeout=min(_COMPACT_BOUNDARY_POLL_SECONDS, remaining)
            )
        except TimeoutError:
            pass
    return None


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

    waiter = register_compact_boundary_waiter(
        session_id, attempt_id, handoff_record_id, getattr(session, "terminal_context", None)
    )
    continuation_pending = False
    detail: dict[str, Any] | None = None
    try:
        for submission in range(_COMPACT_PROVIDER_RETRIES + 1):
            if submission and (
                waiter.event.is_set() or _compact_receipt_exists(db, handoff_record_id, attempt_id)
            ):
                break
            try:
                before_command = await pane.snapshot(CODEX_COMPACT_READY_CAPTURE_LINES)
            except Exception:
                before_command = None
            cursor = _claude_compact_error_cursor(session)
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
                    on_command_submitting=lambda: arm_compact_boundary_waiter(
                        session_id, attempt_id
                    ),
                )
            except Exception:
                if _compact_receipt_exists(db, handoff_record_id, attempt_id):
                    break
                raise
            if not ok:
                if _compact_receipt_exists(db, handoff_record_id, attempt_id):
                    break
                result: dict[str, Any] = {"compacted": False, "reason": reason}
                if detail is not None:
                    result.update(detail)
                return result
            failure = await _wait_for_compact_boundary(waiter, pane, before_command, cursor)
            if (
                failure is None
                or waiter.event.is_set()
                or _compact_receipt_exists(db, handoff_record_id, attempt_id)
            ):
                break
            clear_handoff_compact_continuation_pending(db, session_id, attempt_id=attempt_id)
            if submission == _COMPACT_PROVIDER_RETRIES:
                return {"compacted": False, "reason": failure, "error_code": "compact_failed"}
            logger.warning(
                "Compact handoff for session %s attempt %s failed after submission %d: %s",
                session_id,
                attempt_id,
                submission + 1,
                failure,
            )
            await asyncio.sleep(_COMPACT_RETRY_BACKOFF_SECONDS * (submission + 1))
    except Exception as exc:
        logger.warning(
            "Failed delivering compact handoff for session %s", session_id, exc_info=True
        )
        return {"compacted": False, "reason": str(exc), "error_code": "dispatch_failed"}
    finally:
        unregister_compact_boundary_waiter(session_id, attempt_id)

    if not _compact_receipt_exists(db, handoff_record_id, attempt_id):
        return {"compacted": False, "reason": "compact boundary receipt is missing"}
    clear_queued_context(session_manager, session_id)
    return {
        "compacted": True,
        "command": command,
        "cli": source,
        "via": pane.backend,
        "interrupted": detail is None or detail.get("interrupted") is not False,
        "continuation_pending": continuation_pending,
        "attempt_id": attempt_id,
        "handoff_staged": True,
        "handoff_delivered": True,
    }
