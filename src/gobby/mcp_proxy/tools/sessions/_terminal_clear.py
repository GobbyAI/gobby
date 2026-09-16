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
    _INTERRUPT_OBSERVATION_UNAVAILABLE_ERROR_CODE,
    _authorize_send_keys_target,
    _backfill_tmux_context_from_sibling,
    _interrupt_observer,
    _resolve_pane_io,
    _resolve_session_for_compaction,
    _send_terminal_compaction_command,
)
from gobby.mcp_proxy.tools.sessions._terminal_tmux import composer_reader
from gobby.mcp_proxy.tools.sessions._terminal_webchat import (
    _clear_live_web_chat_fallback,
    _find_live_web_chat_session,
)
from gobby.sessions.clear_continuation import (
    CLEAR_ATTEMPT_VARIABLE,
    clear_failed_attempt,
    mark_clear_command_sent,
    pending_clear_attempt,
    refresh_clear_attempt_content,
    schedule_handoff_continuation,
    stage_clear_attempt,
)
from gobby.sessions.handoff import (
    HandoffAttemptState,
    build_handoff_continue_prompt,
    staged_handoff_tool_result,
)
from gobby.sessions.handoff_records import HandoffPayload
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
    from gobby.terminals.pane_io import PaneIO

logger = logging.getLogger(__name__)

# Successor binding lands 2-3s after staging on an idle daemon (production
# markers: claude 2.59s, grok 2.18s). A missed deadline reverts the staged
# handoff and leaves a late-binding successor unbound, so the window must
# cover a loaded daemon while still bounding the Codex reclaim case.
_CLEAR_ACK_TIMEOUT_SECONDS = 30.0
_CLEAR_ACK_POLL_SECONDS = 0.05
# Codex prints ``codex resume <thread id>`` when the /clear'd thread ends; the
# successor's rollout only appears once its first prompt is typed.
_CODEX_CLEAR_BANNER_TIMEOUT_SECONDS = 10.0
_CODEX_CLEAR_BANNER_POLL_SECONDS = 0.25
_CODEX_CLEAR_BANNER_CAPTURE_LINES = 300
_CODEX_CLEAR_CONTINUE_DELAY_SECONDS = 0.5

CLEAR_COMMAND = "/clear"
_PENDING_ATTEMPT_GUIDANCE = (
    "/clear was delivered after a confirmed interrupt; the successor binds on its "
    "SessionStart. Do not call set_handoff again."
)

__all__ = [
    "CLEAR_COMMAND",
    "deliver_staged_clear_session",
    "execute_clear_session",
    "prepare_clear_session",
]


def _error(message: str, error_code: str) -> dict[str, Any]:
    return {"success": False, "error": message, "error_code": error_code}


def _pending_timeout(session_id: str, attempt_id: str, *, reused_attempt: bool) -> dict[str, Any]:
    """Ack timeout after a delivered /clear: the attempt stays staged for the successor."""
    failure = _error(
        "timed out waiting for clear-session acknowledgment",
        "clear_acknowledgment_timeout",
    )
    failure.update(
        {
            "session_id": session_id,
            "attempt_id": attempt_id,
            "command_sent": True,
            "attempt_restored": False,
            "attempt_pending": True,
            "reused_attempt": reused_attempt,
            "guidance": _PENDING_ATTEMPT_GUIDANCE,
        }
    )
    return failure


def _acknowledged(
    session_id: str,
    attempt_id: str,
    acknowledgment: tuple[str, str],
    *,
    reused_attempt: bool,
) -> dict[str, Any]:
    acknowledged_session_id, acknowledged_by = acknowledgment
    success: dict[str, Any] = {
        "success": True,
        "session_id": session_id,
        "attempt_id": attempt_id,
        "handoff_staged": True,
        "command_sent": True,
        "acknowledged_by": acknowledged_by,
        "reused_attempt": reused_attempt,
    }
    if acknowledged_by == "successor_binding":
        success["successor_id"] = acknowledged_session_id
    else:
        success["observed_session_id"] = acknowledged_session_id
    return success


async def _resume_pending_clear_attempt(
    pending: dict[str, Any],
    handoff: HandoffPayload,
    *,
    db: HubDatabase,
    session_manager: SessionManager,
    session: Any,
) -> dict[str, Any]:
    """Reuse a delivered-but-unacknowledged attempt instead of typing a second /clear.

    The retry never touches the pane: the command was already submitted, and the
    successor binds on its own SessionStart.
    """
    attempt_id = str(pending["attempt_id"])
    refreshed = refresh_clear_attempt_content(
        db,
        session.id,
        attempt_id=attempt_id,
        handoff=handoff,
    )
    identity, baseline_ids = _clear_pane_baseline(session_manager, session)
    acknowledgment = await _wait_for_clear_acknowledgment(
        db,
        session_manager,
        session,
        attempt_id=attempt_id,
        identity=identity,
        baseline_ids=baseline_ids,
    )
    if acknowledgment is None:
        return _pending_timeout(session.id, attempt_id, reused_attempt=True)
    result = _acknowledged(session.id, attempt_id, acknowledgment, reused_attempt=True)
    result["content_refreshed"] = refreshed
    return result


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


async def _codex_thread_end_banner_count(pane: PaneIO, *, thread_id: str) -> int:
    """Count ``codex resume <thread_id>`` on the pane; Codex prints it when that thread ends."""
    snapshot = await pane.snapshot(_CODEX_CLEAR_BANNER_CAPTURE_LINES)
    return (snapshot or "").count(f"codex resume {thread_id}")


async def _wait_for_codex_thread_end(
    pane: PaneIO,
    *,
    thread_id: str,
    baseline: int,
) -> bool:
    """Poll the pane until Codex prints the predecessor's thread-end banner after /clear."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CODEX_CLEAR_BANNER_TIMEOUT_SECONDS
    while True:
        if await _codex_thread_end_banner_count(pane, thread_id=thread_id) > baseline:
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_CODEX_CLEAR_BANNER_POLL_SECONDS, remaining))


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


async def prepare_clear_session(
    handoff: HandoffPayload,
    *,
    session_manager: SessionManager,
    db: HubDatabase,
    agent_run_manager: LocalAgentRunManager,
    web_chat_session_registry: WebChatSessionRegistry | None = None,
    terminal_manager: Any | None = None,
    terminal_runtime_registry: Any | None = None,
) -> dict[str, Any]:
    """Validate and stage a clear attempt without touching terminal input.

    A delivered attempt that has not been acknowledged yet is reused: the handoff
    content is refreshed and the wait resumes, but no second /clear is typed.
    """
    if not handoff.rendered_markdown.strip():
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
        web_result = await _clear_web_chat_session(
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
    pane, error = _resolve_pane_io(
        resolved_session_id,
        session_manager,
        agent_run_manager,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=terminal_runtime_registry,
    )
    if error and not terminal_context_has_tmux_target(session.terminal_context):
        recovered_session = _backfill_tmux_context_from_sibling(
            resolved_session_id,
            session,
            session_manager,
        )
        if recovered_session is not None:
            session = recovered_session
            pane, error = _resolve_pane_io(
                resolved_session_id,
                session_manager,
                agent_run_manager,
                terminal_manager=terminal_manager,
                terminal_runtime_registry=terminal_runtime_registry,
            )
    if error:
        return _error(error, "terminal_target_unavailable")
    assert pane is not None

    if await pane.snapshot(1) is None:
        return _error(
            f"{pane.backend} target {pane.target} is not live",
            "terminal_target_not_live",
        )

    pending = pending_clear_attempt(db, resolved_session_id)
    if pending is not None:
        logger.info(
            "Reusing pending clear attempt %s for session %s",
            pending.get("attempt_id"),
            resolved_session_id,
        )
        return await _resume_pending_clear_attempt(
            pending,
            handoff,
            db=db,
            session_manager=session_manager,
            session=session,
        )

    observe_interrupt, observer_error = _interrupt_observer(source, session)
    if observer_error is not None:
        return _error(observer_error, _INTERRUPT_OBSERVATION_UNAVAILABLE_ERROR_CODE)

    attempt_id = uuid4().hex
    try:
        stage_clear_attempt(
            db,
            resolved_session_id,
            attempt_id=attempt_id,
            handoff=handoff,
            terminal_context=parse_terminal_context_value(session.terminal_context),
            chat_context=None,
        )
    except Exception as exc:
        logger.warning(
            "Failed staging clear-session handoff for session %s",
            resolved_session_id,
            exc_info=True,
        )
        return _error(
            f"failed to stage clear-session handoff: {exc}",
            "staging_failed",
        )

    return staged_handoff_tool_result(
        attempt_id=attempt_id,
        session_id=resolved_session_id,
        clear_session=True,
        command=CLEAR_COMMAND,
        cli=source,
        via=pane.backend,
    )


async def deliver_staged_clear_session(
    session_id: str,
    attempt_id: str,
    *,
    session_manager: SessionManager,
    db: HubDatabase,
    agent_run_manager: LocalAgentRunManager,
    terminal_manager: Any | None = None,
    terminal_runtime_registry: Any | None = None,
) -> dict[str, Any]:
    """Deliver one already-staged /clear attempt after its MCP result completed."""

    def failed(message: str, error_code: str, *, command_sent: bool = False) -> dict[str, Any]:
        failure = _error(message, error_code)
        failure.update(
            {
                "session_id": session_id,
                "attempt_id": attempt_id,
                "command_sent": command_sent,
            }
        )
        return failure

    resolved_session_id, session, error = _resolve_session_for_compaction(
        session_id,
        session_manager,
    )
    if error or session is None or resolved_session_id is None:
        return _error(error or f"Session {session_id} not found", "session_not_found")
    source = getattr(session, "source", None)
    pane, error = _resolve_pane_io(
        resolved_session_id,
        session_manager,
        agent_run_manager,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=terminal_runtime_registry,
    )
    if error:
        return failed(error, "terminal_target_unavailable")
    assert pane is not None
    observe_interrupt, observer_error = _interrupt_observer(source, session)
    if observer_error is not None:
        return failed(observer_error, _INTERRUPT_OBSERVATION_UNAVAILABLE_ERROR_CODE)

    pane_identity, pane_baseline_ids = _clear_pane_baseline(session_manager, session)
    codex_thread_id: str | None = None
    banner_baseline = 0
    if source == "codex":
        thread_id = getattr(session, "external_id", None)
        if not isinstance(thread_id, str) or not thread_id:
            return failed(
                "Codex session has no thread id to confirm /clear",
                "codex_thread_id_unavailable",
            )
        codex_thread_id = thread_id
        banner_baseline = await _codex_thread_end_banner_count(pane, thread_id=thread_id)
    try:
        ok, reason, _pending, failure_detail = await _send_terminal_compaction_command(
            pane,
            CLEAR_COMMAND,
            resolved_session_id,
            cli_source=source if isinstance(source, str) else None,
            mark_continuation_pending=lambda: True,
            clear_continuation_pending=lambda: True,
            observe_interrupt=observe_interrupt,
            composer_read=composer_reader(db, source if isinstance(source, str) else None),
        )
    except Exception as exc:
        logger.warning("Failed sending /clear for session %s", resolved_session_id, exc_info=True)
        return _error(f"failed to send /clear: {exc}", "clear_send_failed")
    if not ok:
        failure = _error(reason or "failed to send /clear", "clear_send_failed")
        if failure_detail is not None:
            failure.update(failure_detail)
        return failure

    if not mark_clear_command_sent(db, resolved_session_id, attempt_id=attempt_id):
        logger.warning(
            "Failed recording /clear delivery on attempt %s for session %s",
            attempt_id,
            resolved_session_id,
        )
    if codex_thread_id is not None:
        if not await _wait_for_codex_thread_end(
            pane,
            thread_id=codex_thread_id,
            baseline=banner_baseline,
        ):
            logger.warning(
                "Codex omitted the thread-end banner for session %s after /clear; "
                "scheduling the continuation from the successful command delivery",
                resolved_session_id,
            )
        if not schedule_handoff_continuation(
            session,
            build_handoff_continue_prompt(),
            delay_seconds=_CODEX_CLEAR_CONTINUE_DELAY_SECONDS,
        ):
            return _pending_timeout(resolved_session_id, attempt_id, reused_attempt=False)
        logger.info(
            "Scheduled Codex clear continuation for session %s after thread %s ended",
            resolved_session_id,
            codex_thread_id,
        )

    acknowledgment = await _wait_for_clear_acknowledgment(
        db,
        session_manager,
        session,
        attempt_id=attempt_id,
        identity=pane_identity,
        baseline_ids=pane_baseline_ids,
    )
    if acknowledgment is None:
        return _pending_timeout(resolved_session_id, attempt_id, reused_attempt=False)
    return _acknowledged(
        resolved_session_id,
        attempt_id,
        acknowledgment,
        reused_attempt=False,
    )


async def execute_clear_session(
    handoff: HandoffPayload,
    *,
    session_manager: SessionManager,
    db: HubDatabase,
    agent_run_manager: LocalAgentRunManager,
    web_chat_session_registry: WebChatSessionRegistry | None = None,
    terminal_manager: Any | None = None,
    terminal_runtime_registry: Any | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper that prepares and settles a clear delivery."""
    prepared = await prepare_clear_session(
        handoff,
        session_manager=session_manager,
        db=db,
        agent_run_manager=agent_run_manager,
        web_chat_session_registry=web_chat_session_registry,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=terminal_runtime_registry,
    )
    if prepared.get("delivery_pending") is not True:
        return prepared
    session_id = prepared["session_id"]
    attempt_id = prepared["attempt_id"]
    try:
        result = await shielded_terminal_delivery(
            f"clear-session:{session_id}",
            lambda: deliver_staged_clear_session(
                session_id,
                attempt_id,
                session_manager=session_manager,
                db=db,
                agent_run_manager=agent_run_manager,
                terminal_manager=terminal_manager,
                terminal_runtime_registry=terminal_runtime_registry,
            ),
            raise_if_closed=True,
        )
    except TerminalDeliveryAdmissionClosedError as exc:
        result = _error(str(exc), "clear_delivery_unavailable")
    if result.get("success") is not True and result.get("attempt_pending") is not True:
        restored = clear_failed_attempt(
            db,
            session_id,
            attempt_id=attempt_id,
        )
        if "command_sent" in result:
            result["attempt_restored"] = restored
    return result


async def _clear_web_chat_session(
    handoff: HandoffPayload,
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
            handoff=handoff,
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
