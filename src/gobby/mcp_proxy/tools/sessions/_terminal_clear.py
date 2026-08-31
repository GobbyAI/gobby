"""Clear-session dispatch for the structured set_handoff tool."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from gobby.agents.terminal_delivery import (
    TerminalDeliveryAdmissionClosedError,
    shielded_terminal_delivery,
)
from gobby.mcp_proxy.tools.sessions._terminal import (
    _authorize_send_keys_target,
    _backfill_tmux_context_from_sibling,
    _resolve_session_for_compaction,
    _resolve_tmux_target,
    _send_terminal_compaction_command,
)
from gobby.mcp_proxy.tools.sessions._terminal_webchat import (
    _clear_live_web_chat_fallback,
    _find_live_web_chat_session,
)
from gobby.sessions.clear_continuation import (
    CLEAR_ATTEMPT_VARIABLE,
    clear_failed_attempt,
    stage_clear_attempt,
)
from gobby.sessions.compact_continuation import (
    CodexRolloutCursor,
    CodexRolloutObservationError,
)
from gobby.sessions.handoff import (
    FeedbackObservation,
    HandoffAttemptState,
    build_handoff_continue_prompt,
)
from gobby.terminal_context import (
    parse_terminal_context_value,
    terminal_context_has_tmux_target,
)
from gobby.terminal_ownership import terminal_session_identity
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

_CLEAR_ACK_TIMEOUT_SECONDS = 5.0
_CLEAR_ACK_POLL_SECONDS = 0.05

CLEAR_COMMAND = "/clear"

__all__ = ["CLEAR_COMMAND", "execute_clear_session"]


def _error(message: str, error_code: str) -> dict[str, Any]:
    return {"success": False, "error": message, "error_code": error_code}


def _clear_pane_baseline(
    session_manager: SessionManager,
    predecessor: Any,
) -> tuple[Any | None, frozenset[str]]:
    """Capture the terminal identity and rows that predate clear dispatch."""
    identity = terminal_session_identity(predecessor)
    session_ids = {predecessor.id}
    if identity is None:
        return None, frozenset(session_ids)
    try:
        session_ids.update(
            session.id for session in session_manager.find_by_terminal_identity(identity)
        )
    except Exception:
        logger.warning(
            "Failed capturing terminal-clear pane baseline for session %s",
            predecessor.id,
            exc_info=True,
        )
    return identity, frozenset(session_ids)


def _find_new_provider_session(
    session_manager: SessionManager,
    predecessor: Any,
    identity: Any | None,
    baseline_ids: frozenset[str],
) -> str | None:
    """Return a fresh provider-native row observed on the clear target pane."""
    if identity is None:
        return None
    try:
        candidates = session_manager.find_by_terminal_identity(identity)
    except Exception:
        logger.warning(
            "Failed observing terminal-clear pane for session %s",
            predecessor.id,
            exc_info=True,
        )
        return None
    for candidate in reversed(candidates):
        if candidate.id in baseline_ids or candidate.source != predecessor.source:
            continue
        external_changed = bool(
            candidate.external_id and candidate.external_id != predecessor.external_id
        )
        rollout_changed = bool(
            predecessor.source == "codex"
            and candidate.transcript_path
            and candidate.transcript_path != predecessor.transcript_path
        )
        if external_changed or rollout_changed:
            return candidate.id
    return None


async def _wait_for_clear_acknowledgment(
    db: HubDatabase,
    session_manager: SessionManager,
    predecessor: Any,
    *,
    attempt_id: str,
    identity: Any | None,
    baseline_ids: frozenset[str],
) -> tuple[str, str] | None:
    """Wait for marker consumption or a fresh provider session on the pane."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CLEAR_ACK_TIMEOUT_SECONDS
    variables_manager = SessionVariableManager(db)
    while True:
        try:
            variables = variables_manager.get_variables(predecessor.id)
        except Exception:
            logger.debug(
                "Clear acknowledgment marker observation failed for session %s",
                predecessor.id,
                exc_info=True,
            )
            variables = {}
        marker = variables.get(CLEAR_ATTEMPT_VARIABLE)
        successor_id = marker.get("consumed_by") if isinstance(marker, dict) else None
        marker_attempt_id = marker.get("attempt_id") if isinstance(marker, dict) else None
        if isinstance(successor_id, str) and successor_id and marker_attempt_id == attempt_id:
            return successor_id, "successor_binding"
        observed_id = _find_new_provider_session(
            session_manager,
            predecessor,
            identity,
            baseline_ids,
        )
        if observed_id is not None:
            return observed_id, "provider_session"
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(_CLEAR_ACK_POLL_SECONDS, remaining))


async def execute_clear_session(
    handoff_markdown: str,
    observations: list[FeedbackObservation],
    *,
    session_manager: SessionManager,
    db: HubDatabase,
    agent_run_manager: LocalAgentRunManager,
    web_chat_session_registry: WebChatSessionRegistry | None = None,
) -> dict[str, Any]:
    """Stage a clear attempt, then deliver /clear through the compaction sender."""
    if not handoff_markdown.strip():
        return _error("set_handoff requires rendered handoff content", "handoff_required")

    session_id = get_current_session_id()
    if not session_id:
        return _error(
            "set_handoff requires current MCP SessionContext",
            "session_context_required",
        )

    resolved_session_id, session, error = _resolve_session_for_compaction(
        session_id,
        session_manager,
    )
    if error:
        web_result = await _clear_web_chat_session(
            handoff_markdown,
            observations,
            db=db,
            session_manager=session_manager,
            agent_run_manager=agent_run_manager,
            web_chat_session_registry=web_chat_session_registry,
            session_ids=(session_id, resolved_session_id),
            db_session=None,
        )
        if web_result is not None:
            return web_result
        return _error(error, "session_not_found")
    assert resolved_session_id is not None
    assert session is not None

    session_type = getattr(session, "session_type", "terminal")
    if session_type == "web_chat":
        if getattr(session, "status", None) == "deleted":
            return _error(
                f"Session {resolved_session_id} is deleted",
                "session_deleted",
            )
        web_result = await _clear_web_chat_session(
            handoff_markdown,
            observations,
            db=db,
            session_manager=session_manager,
            agent_run_manager=agent_run_manager,
            web_chat_session_registry=web_chat_session_registry,
            session_ids=(resolved_session_id, session_id),
            db_session=session,
        )
        if web_result is not None:
            return web_result
        return _error(
            f"No live web_chat session found for {resolved_session_id}",
            "web_chat_not_live",
        )
    if session_type != "terminal":
        return _error(
            f"unsupported session_type: {session_type}",
            "unsupported_session_type",
        )
    if agent_run_manager.get_by_session(resolved_session_id) is not None:
        return _error(
            "set_handoff(clear_session=true) is not supported for agent-run sessions",
            "agent_run_unsupported",
        )
    if getattr(session, "status", None) == "deleted":
        return _error(
            f"Session {resolved_session_id} is deleted",
            "session_deleted",
        )

    _authorized_id, authorization_error = _authorize_send_keys_target(
        resolved_session_id,
        session_manager,
    )
    if authorization_error is not None:
        return authorization_error

    source = getattr(session, "source", None)
    target, tmux, error = _resolve_tmux_target(
        resolved_session_id,
        session_manager,
        agent_run_manager,
    )
    if error and not terminal_context_has_tmux_target(session.terminal_context):
        recovered_session = _backfill_tmux_context_from_sibling(
            resolved_session_id,
            session,
            session_manager,
        )
        if recovered_session is not None:
            session = recovered_session
            target, tmux, error = _resolve_tmux_target(
                resolved_session_id,
                session_manager,
                agent_run_manager,
            )
    if error:
        return _error(error, "tmux_target_unavailable")
    assert target is not None
    assert tmux is not None

    try:
        pane_probe = await tmux.capture_pane(target, lines=1)
    except Exception as exc:
        logger.warning(
            "Failed verifying clear-session tmux target %s for session %s",
            target,
            resolved_session_id,
            extra={
                "event": "clear_session_tmux_target_verification_failed",
                "session_id": resolved_session_id,
                "tmux_target": target,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            exc_info=True,
        )
        return _error(
            f"failed to verify live tmux target {target}: {exc}",
            "tmux_target_verification_failed",
        )
    if pane_probe is None:
        return _error(
            f"tmux target {target} is not live",
            "tmux_target_not_live",
        )

    observe_codex_interrupt = _codex_interrupt_observer(source, session)
    if source == "codex" and observe_codex_interrupt is None:
        return _error(
            "Codex rollout transcript is unavailable for interrupt confirmation",
            "codex_interrupt_observation_unavailable",
        )

    pane_identity, pane_baseline_ids = _clear_pane_baseline(session_manager, session)
    attempt_id = uuid4().hex
    staged = False
    attempt_state: HandoffAttemptState | None = None
    try:
        attempt_state = stage_clear_attempt(
            db,
            resolved_session_id,
            attempt_id=attempt_id,
            handoff_markdown=handoff_markdown,
            observations=observations,
            terminal_context=parse_terminal_context_value(session.terminal_context),
            chat_context=None,
        )
        staged = True
    except Exception as exc:
        logger.warning(
            "Failed staging clear-session handoff for session %s",
            resolved_session_id,
            exc_info=True,
        )
        if staged:
            clear_failed_attempt(
                db,
                resolved_session_id,
                attempt_id=attempt_id,
                attempt_state=attempt_state,
            )
        return _error(
            f"failed to stage clear-session handoff: {exc}",
            "staging_failed",
        )

    def restore_failed_attempt() -> bool:
        return clear_failed_attempt(
            db,
            resolved_session_id,
            attempt_id=attempt_id,
            attempt_state=attempt_state,
        )

    async def deliver_clear() -> dict[str, Any]:
        try:
            ok, reason, _pending, failure_detail = await _send_terminal_compaction_command(
                tmux,
                target,
                CLEAR_COMMAND,
                resolved_session_id,
                cli_source=source if isinstance(source, str) else None,
                mark_continuation_pending=lambda: True,
                clear_continuation_pending=restore_failed_attempt,
                observe_codex_interrupt=observe_codex_interrupt,
            )
        except Exception as exc:
            restore_failed_attempt()
            logger.warning(
                "Failed sending /clear for session %s",
                resolved_session_id,
                exc_info=True,
            )
            return _error(f"failed to send /clear: {exc}", "clear_send_failed")

        if not ok:
            restore_failed_attempt()
            failure: dict[str, Any] = _error(
                reason or "failed to send /clear",
                "clear_send_failed",
            )
            if failure_detail is not None:
                failure.update(failure_detail)
            return failure

        acknowledgment = await _wait_for_clear_acknowledgment(
            db,
            session_manager,
            session,
            attempt_id=attempt_id,
            identity=pane_identity,
            baseline_ids=pane_baseline_ids,
        )
        if acknowledgment is None:
            restored = restore_failed_attempt()
            failure = _error(
                "timed out waiting for clear-session acknowledgment",
                "clear_acknowledgment_timeout",
            )
            failure.update(
                {
                    "session_id": resolved_session_id,
                    "attempt_id": attempt_id,
                    "command_sent": True,
                    "attempt_restored": restored,
                }
            )
            return failure

        acknowledged_session_id, acknowledged_by = acknowledgment
        success = {
            "success": True,
            "session_id": resolved_session_id,
            "attempt_id": attempt_id,
            "handoff_staged": True,
            "command_sent": True,
            "acknowledged_by": acknowledged_by,
        }
        if acknowledged_by == "successor_binding":
            success["successor_id"] = acknowledged_session_id
        else:
            success["observed_session_id"] = acknowledged_session_id
        return success

    try:
        result = await shielded_terminal_delivery(
            f"clear-session:{resolved_session_id}",
            deliver_clear,
            raise_if_closed=True,
        )
    except TerminalDeliveryAdmissionClosedError as exc:
        restore_failed_attempt()
        return _error(str(exc), "clear_delivery_unavailable")
    assert result is not None
    return result


async def _clear_web_chat_session(
    handoff_markdown: str,
    observations: list[FeedbackObservation],
    *,
    db: HubDatabase,
    session_manager: SessionManager,
    agent_run_manager: LocalAgentRunManager,
    web_chat_session_registry: WebChatSessionRegistry | None,
    session_ids: tuple[str | None, ...],
    db_session: Any | None,
) -> dict[str, Any] | None:
    """Stage a durable web-chat clear attempt, then delegate to the live registry."""
    if web_chat_session_registry is None:
        if db_session is not None:
            return _error(
                "web_chat session registry is not available",
                "web_chat_registry_unavailable",
            )
        return None

    _lookup_id, live = _find_live_web_chat_session(web_chat_session_registry, *session_ids)
    if live is None:
        if db_session is not None:
            predecessor = getattr(db_session, "id", None) or session_ids[0]
            return _error(
                f"No live web_chat session found for {predecessor}",
                "web_chat_not_live",
            )
        return None

    predecessor_id = getattr(live, "db_session_id", None)
    if not isinstance(predecessor_id, str) or not predecessor_id:
        predecessor_id = getattr(db_session, "id", None)
    if not isinstance(predecessor_id, str) or not predecessor_id:
        predecessor_id = _lookup_id
    if not isinstance(predecessor_id, str) or not predecessor_id:
        return None

    if agent_run_manager.get_by_session(predecessor_id) is not None:
        return _error(
            "set_handoff(clear_session=true) is not supported for agent-run sessions",
            "agent_run_unsupported",
        )

    attempt_id = uuid4().hex
    staged = False
    attempt_state: HandoffAttemptState | None = None
    try:
        attempt_state = stage_clear_attempt(
            db,
            predecessor_id,
            attempt_id=attempt_id,
            handoff_markdown=handoff_markdown,
            observations=observations,
            terminal_context=None,
            chat_context=_web_chat_attempt_context(live, db_session),
        )
        staged = True
    except Exception as exc:
        logger.warning(
            "Failed staging web-chat clear handoff for session %s",
            predecessor_id,
            exc_info=True,
        )
        if staged:
            clear_failed_attempt(
                db,
                predecessor_id,
                attempt_id=attempt_id,
                attempt_state=attempt_state,
            )
        return _error(
            f"failed to stage web-chat clear handoff: {exc}",
            "staging_failed",
        )

    try:
        result = await _clear_live_web_chat_fallback(
            web_chat_session_registry,
            *session_ids,
            attempt_id=attempt_id,
            continuation_prompt=build_handoff_continue_prompt(),
        )
    except Exception as exc:
        clear_failed_attempt(
            db,
            predecessor_id,
            attempt_id=attempt_id,
            attempt_state=attempt_state,
        )
        logger.warning(
            "Failed clearing live web_chat session %s",
            predecessor_id,
            exc_info=True,
        )
        return _error(f"failed to clear web chat: {exc}", "web_chat_clear_failed")

    if result is None:
        clear_failed_attempt(
            db,
            predecessor_id,
            attempt_id=attempt_id,
            attempt_state=attempt_state,
        )
        return _error(
            f"No live web_chat session found for {predecessor_id}",
            "web_chat_not_live",
        )

    if result.get("queued"):
        queued_attempt = result.get("attempt_id")
        return {
            "queued": True,
            "attempt_id": queued_attempt if isinstance(queued_attempt, str) else attempt_id,
            "handoff_staged": True,
        }

    if result.get("cleared"):
        return {
            **result,
            "success": True,
            "session_id": result.get("predecessor_id") or predecessor_id,
            "attempt_id": result.get("attempt_id") or attempt_id,
            "handoff_staged": True,
        }

    clear_failed_attempt(
        db,
        predecessor_id,
        attempt_id=attempt_id,
        attempt_state=attempt_state,
    )
    return _error(
        str(result.get("reason") or "web chat clear failed"),
        "web_chat_clear_failed",
    )


def _web_chat_attempt_context(live: Any, db_session: Any | None) -> dict[str, Any] | None:
    model = getattr(live, "model", None)
    if not isinstance(model, str) or not model:
        model = getattr(db_session, "model", None)
    mode = getattr(live, "chat_mode", None)
    if not isinstance(mode, str) or not mode:
        mode = getattr(db_session, "chat_mode", None)
    payload: dict[str, Any] = {}
    if isinstance(model, str) and model:
        payload["model"] = model
    if isinstance(mode, str) and mode:
        payload["mode"] = mode
    return payload or None


def _codex_interrupt_observer(
    source: Any,
    session: Any,
) -> Any | None:
    if source != "codex":
        return None
    try:
        cursor = CodexRolloutCursor.at_eof(getattr(session, "transcript_path", None))
    except CodexRolloutObservationError as exc:
        logger.warning(
            "Cannot observe Codex interruption for clear-session handoff %s: %s",
            getattr(session, "id", None),
            exc,
        )
        return None

    def observe_codex_rollout_interrupt() -> bool | None:
        try:
            return cursor.saw_fresh_turn_aborted()
        except CodexRolloutObservationError as observe_exc:
            logger.warning(
                "Lost Codex interrupt observation for clear-session handoff %s: %s",
                getattr(session, "id", None),
                observe_exc,
            )
            return None

    return observe_codex_rollout_interrupt
