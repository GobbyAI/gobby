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
from gobby.sessions.clear_continuation import clear_failed_attempt, stage_clear_attempt
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
            "sessions are rejected. Web-chat clear is not yet supported."
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


def _web_chat_unsupported() -> dict[str, Any]:
    return {
        "success": False,
        "error": "web chat not yet supported",
        "error_code": "web_chat_not_supported",
    }


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
        if _live_web_chat_session(web_chat_session_registry, session_id, resolved_session_id):
            return _web_chat_unsupported()
        return _error(error, "session_not_found")
    assert resolved_session_id is not None
    assert session is not None

    session_type = getattr(session, "session_type", "terminal")
    if session_type == "web_chat":
        return _web_chat_unsupported()
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


def _live_web_chat_session(
    web_chat_session_registry: WebChatSessionRegistry | None,
    *session_ids: str | None,
) -> bool:
    if web_chat_session_registry is None:
        return False
    seen: set[str] = set()
    for candidate in session_ids:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            live = web_chat_session_registry.find_session(candidate)[1]
        except (LookupError, KeyError, RuntimeError):
            continue
        if live is not None:
            return True
    return False


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
