"""clear_self MCP tool: stage a durable handoff, then send /clear."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

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
    build_clear_self_continue_prompt,
    clear_failed_attempt,
    stage_clear_attempt,
)
from gobby.sessions.compact_continuation import (
    CodexRolloutCursor,
    CodexRolloutObservationError,
)
from gobby.terminal_context import (
    parse_terminal_context_value,
    terminal_context_has_tmux_target,
)
from gobby.utils.session_context import get_current_session_id

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

CLEAR_COMMAND = "/clear"

__all__ = ["CLEAR_COMMAND", "execute_clear_self", "register_clear_self_tool"]


def register_clear_self_tool(
    registry: InternalToolRegistry,
    *,
    session_manager: SessionManager,
    db: HubDatabase,
    agent_run_manager: LocalAgentRunManager,
    web_chat_session_registry: WebChatSessionRegistry | None = None,
) -> None:
    """Register clear_self on the gobby-sessions terminal tool surface."""

    @registry.tool(
        name="clear_self",
        description=(
            "Clear your own context and continue from a caller-authored handoff. "
            "Requires a non-empty `handoff`; empty or whitespace is an error, and "
            "there is no digest or summarizer fallback. In a terminal session this "
            "call does not return normally: the Gobby daemon interrupts your active "
            "turn and types `/clear` into your pane, so your CLI reports the call "
            "as rejected or cancelled. That attribution is wrong — the daemon did "
            "it. Do not stop, and do not ask the user what happened. Agent-run "
            "sessions are rejected. Web-chat sessions clear through the live "
            "daemon ChatSession registry and do return normally."
        ),
    )
    async def clear_self(handoff: str) -> dict[str, Any]:
        return await execute_clear_self(
            handoff,
            session_manager=session_manager,
            db=db,
            agent_run_manager=agent_run_manager,
            web_chat_session_registry=web_chat_session_registry,
        )


def _error(message: str, error_code: str) -> dict[str, Any]:
    return {"success": False, "error": message, "error_code": error_code}


async def execute_clear_self(
    handoff: str,
    *,
    session_manager: SessionManager,
    db: HubDatabase,
    agent_run_manager: LocalAgentRunManager,
    web_chat_session_registry: WebChatSessionRegistry | None = None,
) -> dict[str, Any]:
    """Stage a clear attempt, then deliver /clear through the compaction sender."""
    if not isinstance(handoff, str) or not handoff.strip():
        return _error("clear_self requires a non-empty handoff", "handoff_required")

    session_id = get_current_session_id()
    if not session_id:
        return _error(
            "clear_self requires current MCP SessionContext",
            "session_context_required",
        )

    resolved_session_id, session, error = _resolve_session_for_compaction(
        session_id,
        session_manager,
    )
    if error:
        web_result = await _clear_web_chat_self(
            handoff,
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
        web_result = await _clear_web_chat_self(
            handoff,
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
            "clear_self is not supported for agent-run sessions",
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
            "Failed verifying clear_self tmux target %s for session %s",
            target,
            resolved_session_id,
            extra={
                "event": "clear_self_tmux_target_verification_failed",
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

    attempt_id = uuid4().hex
    staged = False
    prior_summary_state: dict[str, Any] = {}
    try:
        prior_summary_state = stage_clear_attempt(
            db,
            resolved_session_id,
            attempt_id=attempt_id,
            terminal_context=parse_terminal_context_value(session.terminal_context),
            chat_context=None,
        )
        staged = True
        updated = session_manager.update_summary(
            resolved_session_id,
            summary_markdown=handoff,
        )
        if updated is None:
            raise RuntimeError(
                f"failed to persist clear_self handoff for session {resolved_session_id}"
            )
    except Exception as exc:
        logger.warning(
            "Failed staging clear_self handoff for session %s",
            resolved_session_id,
            exc_info=True,
        )
        if staged:
            clear_failed_attempt(
                db,
                resolved_session_id,
                attempt_id=attempt_id,
                prior_summary_state=prior_summary_state,
            )
        return _error(
            f"failed to stage clear_self handoff: {exc}",
            "staging_failed",
        )

    def restore_failed_attempt() -> bool:
        return clear_failed_attempt(
            db,
            resolved_session_id,
            attempt_id=attempt_id,
            prior_summary_state=prior_summary_state,
        )

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

    return {
        "success": True,
        "session_id": resolved_session_id,
        "attempt_id": attempt_id,
        "handoff_staged": True,
        "command_sent": True,
    }


async def _clear_web_chat_self(
    handoff: str,
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
            "clear_self is not supported for agent-run sessions",
            "agent_run_unsupported",
        )

    attempt_id = uuid4().hex
    staged = False
    prior_summary_state: dict[str, Any] = {}
    try:
        prior_summary_state = stage_clear_attempt(
            db,
            predecessor_id,
            attempt_id=attempt_id,
            terminal_context=None,
            chat_context=_web_chat_attempt_context(live, db_session),
        )
        staged = True
        updated = session_manager.update_summary(
            predecessor_id,
            summary_markdown=handoff,
        )
        if updated is None:
            raise RuntimeError(f"failed to persist clear_self handoff for session {predecessor_id}")
    except Exception as exc:
        logger.warning(
            "Failed staging web_chat clear_self handoff for session %s",
            predecessor_id,
            exc_info=True,
        )
        if staged:
            clear_failed_attempt(
                db,
                predecessor_id,
                attempt_id=attempt_id,
                prior_summary_state=prior_summary_state,
            )
        return _error(
            f"failed to stage clear_self handoff: {exc}",
            "staging_failed",
        )

    try:
        result = await _clear_live_web_chat_fallback(
            web_chat_session_registry,
            *session_ids,
            attempt_id=attempt_id,
            continuation_prompt=build_clear_self_continue_prompt(predecessor_ref=predecessor_id),
        )
    except Exception as exc:
        clear_failed_attempt(
            db,
            predecessor_id,
            attempt_id=attempt_id,
            prior_summary_state=prior_summary_state,
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
            prior_summary_state=prior_summary_state,
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
        prior_summary_state=prior_summary_state,
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
            "Cannot observe Codex interruption for clear_self session %s: %s",
            getattr(session, "id", None),
            exc,
        )
        return None

    def observe_codex_rollout_interrupt() -> bool | None:
        try:
            return cursor.saw_fresh_turn_aborted()
        except CodexRolloutObservationError as observe_exc:
            logger.warning(
                "Lost Codex interrupt observation for clear_self session %s: %s",
                getattr(session, "id", None),
                observe_exc,
            )
            return None

    return observe_codex_rollout_interrupt
