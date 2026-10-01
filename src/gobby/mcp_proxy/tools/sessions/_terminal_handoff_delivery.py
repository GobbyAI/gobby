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
    _turn_settled_observer,
)
from gobby.mcp_proxy.tools.sessions._terminal_compaction import (
    _CLI_COMPACT_COMMANDS,
    NO_TERMINAL_TARGET_ERROR_CODE,
    _fresh_output_delta,
    composer_reader,
)
from gobby.sessions.compact_continuation import (
    CODEX_COMPACT_READY_CAPTURE_LINES,
    CompactBoundaryWaiter,
    arm_compact_boundary_waiter,
    clear_handoff_compact_continuation_pending,
    consume_and_schedule_handoff_compact_continuation,
    mark_handoff_compact_continuation_pending,
    register_compact_boundary_waiter,
    schedule_codex_handoff_compact_continuation_readiness,
    unregister_compact_boundary_waiter,
)
from gobby.sessions.handoff import build_handoff_continue_prompt
from gobby.sessions.transcript_cursor import (
    CodexRolloutCursor,
    TranscriptObservationError,
    TranscriptTailCursor,
)
from gobby.terminal_ownership import recorded_seat_left

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

_COMPACT_BOUNDARY_CONFIRM_SECONDS = 600.0
_COMPACT_BOUNDARY_POLL_SECONDS = 2.0
_COMPACT_ERROR_PREFIX = "Error during compaction:"
_COMPACT_BOUNDARY_TIMEOUT_REASON = (
    "compact boundary was not observed before the confirmation deadline"
)


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
    *,
    timeout_seconds: float | None = None,
    codex_cursor: CodexRolloutCursor | None = None,
    on_codex_boundary: Callable[[], bool] | None = None,
) -> str | None:
    """Return an error or wait for one boundary within this delivery operation."""
    deadline = asyncio.get_running_loop().time() + (
        _COMPACT_BOUNDARY_CONFIRM_SECONDS if timeout_seconds is None else timeout_seconds
    )
    while not waiter.event.is_set():
        if codex_cursor is not None:
            try:
                if await asyncio.to_thread(codex_cursor.saw_fresh_compacted):
                    if on_codex_boundary is not None:
                        try:
                            await asyncio.to_thread(on_codex_boundary)
                        except Exception:
                            logger.warning(
                                "Failed settling Codex rollout compact boundary", exc_info=True
                            )
            except TranscriptObservationError:
                logger.warning("Codex compact rollout observation ended", exc_info=True)
                codex_cursor = None
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
            return _COMPACT_BOUNDARY_TIMEOUT_REASON
        try:
            await asyncio.wait_for(
                waiter.event.wait(), timeout=min(_COMPACT_BOUNDARY_POLL_SECONDS, remaining)
            )
        except TimeoutError:
            pass
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
    if recorded_seat_left(session):
        return {
            "compacted": False,
            "reason": "the recorded CLI process no longer owns its terminal",
            "error_code": NO_TERMINAL_TARGET_ERROR_CODE,
        }
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
    codex_cursor: CodexRolloutCursor | None = None

    def command_submitting() -> None:
        nonlocal codex_cursor
        if source == "codex":
            try:
                codex_cursor = CodexRolloutCursor.at_eof(getattr(session, "transcript_path", None))
            except TranscriptObservationError:
                logger.warning("Codex compact rollout is unavailable for session %s", session_id)
        arm_compact_boundary_waiter(session_id, attempt_id)

    continuation_pending = False
    detail: dict[str, Any] | None = None
    failure_result: dict[str, Any] | None = None
    try:
        try:
            before_command = await pane.snapshot(CODEX_COMPACT_READY_CAPTURE_LINES)
        except Exception:
            before_command = None
        cursor = _claude_compact_error_cursor(session)
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
            on_command_submitting=command_submitting,
            seat_left=lambda: recorded_seat_left(session),
        )
        if not ok and not _compact_receipt_exists(db, handoff_record_id, attempt_id):
            failure_result = {"compacted": False, "reason": reason}
            if detail is not None:
                failure_result.update(detail)
        elif ok:
            delivery_loop = asyncio.get_running_loop()
            failure = await _wait_for_compact_boundary(
                waiter,
                pane,
                before_command,
                cursor,
                codex_cursor=codex_cursor,
                on_codex_boundary=lambda: consume_and_schedule_handoff_compact_continuation(
                    db,
                    pending_session_id=session_id,
                    target_session=session,
                    loop=delivery_loop,
                    terminal_manager=terminal_manager,
                    terminal_runtime_registry=terminal_runtime_registry,
                ),
            )
            if (
                failure is not None
                and not waiter.event.is_set()
                and not _compact_receipt_exists(db, handoff_record_id, attempt_id)
            ):
                unconfirmed = failure == _COMPACT_BOUNDARY_TIMEOUT_REASON
                if not unconfirmed:
                    clear_handoff_compact_continuation_pending(
                        db, session_id, attempt_id=attempt_id
                    )
                failure_result = {
                    "compacted": False,
                    "reason": failure,
                    "error_code": "compact_unconfirmed" if unconfirmed else "compact_failed",
                }
    except Exception as exc:
        logger.warning(
            "Failed delivering compact handoff for session %s", session_id, exc_info=True
        )
        failure_result = {"compacted": False, "reason": str(exc), "error_code": "dispatch_failed"}
    finally:
        unregister_compact_boundary_waiter(session_id, attempt_id)

    if not _compact_receipt_exists(db, handoff_record_id, attempt_id):
        return failure_result or {
            "compacted": False,
            "reason": "compact boundary receipt is missing",
        }
    if source == "codex" and not schedule_codex_handoff_compact_continuation_readiness(
        db,
        pane=pane,
        pending_session_id=session_id,
        before_command=before_command,
        attempt_id=attempt_id,
    ):
        consume_and_schedule_handoff_compact_continuation(
            db,
            pending_session_id=session_id,
            target_session=session,
            terminal_manager=terminal_manager,
            terminal_runtime_registry=terminal_runtime_registry,
        )
    clear_queued_context(session_manager, session_id)
    delivered: dict[str, Any] = {
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
    if detail is not None and detail.get("submit_unverified"):
        # No composer read proved the command submitted; the compact receipt above did.
        delivered["submit_unverified"] = True
    return delivered
