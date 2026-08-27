"""Terminal interaction tools for tmux-backed sessions.

Exposes send_keys, capture_output, compact_self, and clear_self as MCP tools on
gobby-sessions, enabling orchestration (heartbeat, pipelines, other agents)
to interact with running terminal sessions.
"""

from __future__ import annotations

import asyncio as asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.mcp_proxy.tools.sessions._terminal_handoff import (
    _COMPACT_HANDOFF_FALLBACK_MAX_CHARS,
    _DEFAULT_COMPACT_HANDOFF_REFRESH_TIMEOUT_SECONDS,
    _capture_handoff_configs,
    _compact_handoff_digest_fallback_markdown,
    _compact_handoff_refresh_timeout_seconds,
    _compact_handoff_transcript_tail_markdown,
    _has_summary_refresh_source,
    _mark_compact_handoff_ready,
    _persist_compact_handoff_fallback,
    _refresh_compact_handoff_context,
    _run_compact_handoff_background_refresh,
    _schedule_compact_handoff_background_refresh,
    _valid_existing_summary_markdown,
)
from gobby.mcp_proxy.tools.sessions._terminal_tmux import (
    _CLI_COMPACT_COMMANDS,
    _CLI_COMPACT_INTERRUPT_KEYS,
    _CODEX_INTERRUPT_SETTLE_SECONDS,
    _COMPACTION_REJECTION_CAPTURE_LINES,
    _COMPACTION_REJECTION_ERROR_CODE,
    _COMPACTION_REJECTION_SETTLE_SECONDS,
    _DEFAULT_COMPACT_INTERRUPT_KEY,
    _DEFAULT_INTERRUPT_SETTLE_SECONDS,
    _capture_pane_snapshot,
    _compact_interrupt_key,
    _detect_compaction_rejection,
    _fresh_output_delta,
    _send_compaction_command,
    _send_tmux_keys,
)
from gobby.mcp_proxy.tools.sessions._terminal_tmux import (
    _resolve_tmux_target as _resolve_tmux_target_impl,
)
from gobby.mcp_proxy.tools.sessions._terminal_tmux import (
    _send_terminal_compaction_command as _send_terminal_compaction_command_impl,
)
from gobby.mcp_proxy.tools.sessions._terminal_transcripts import (
    _TRANSCRIPT_TAIL_MAX_BYTES,
    _capture_transcript_tail,
    _read_transcript_tail_lines,
)
from gobby.mcp_proxy.tools.sessions._terminal_webchat import _compact_live_web_chat_fallback
from gobby.sessions.compact_continuation import (
    CODEX_COMPACT_READY_CAPTURE_LINES,
    CodexRolloutCursor,
    CodexRolloutObservationError,
    build_compact_self_continue_prompt,
    clear_compact_self_continuation_pending,
    mark_compact_self_continuation_pending,
    persist_compact_resume_required_skills,
    schedule_codex_compact_self_continuation_readiness,
)
from gobby.sessions.tmux_context import get_tmux_manager_for_context
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.session_activity import reconcile_compact_session_activity
from gobby.terminal_context import parse_terminal_context_value, terminal_context_has_tmux_target

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.config.sessions import SessionSummaryConfig
    from gobby.config.tasks import CompactHandoffConfig
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

__all__ = [
    "_CLI_COMPACT_COMMANDS",
    "_CLI_COMPACT_INTERRUPT_KEYS",
    "_CODEX_INTERRUPT_SETTLE_SECONDS",
    "_COMPACT_HANDOFF_FALLBACK_MAX_CHARS",
    "_COMPACTION_REJECTION_CAPTURE_LINES",
    "_COMPACTION_REJECTION_ERROR_CODE",
    "_COMPACTION_REJECTION_SETTLE_SECONDS",
    "_DEFAULT_COMPACT_INTERRUPT_KEY",
    "_DEFAULT_COMPACT_HANDOFF_REFRESH_TIMEOUT_SECONDS",
    "_TRANSCRIPT_TAIL_MAX_BYTES",
    "_capture_pane_snapshot",
    "_capture_transcript_tail",
    "_compact_handoff_digest_fallback_markdown",
    "_compact_handoff_refresh_timeout_seconds",
    "_compact_handoff_transcript_tail_markdown",
    "_compact_interrupt_key",
    "_compact_live_web_chat_fallback",
    "_detect_compaction_rejection",
    "_fresh_output_delta",
    "_has_summary_refresh_source",
    "_mark_compact_handoff_ready",
    "_persist_compact_handoff_fallback",
    "_read_transcript_tail_lines",
    "_refresh_compact_handoff_context",
    "_resolve_session_for_compaction",
    "_resolve_tmux_target",
    "_run_compact_handoff_background_refresh",
    "_schedule_compact_handoff_background_refresh",
    "_send_compaction_command",
    "_send_terminal_compaction_command",
    "_send_tmux_keys",
    "_valid_existing_summary_markdown",
    "asyncio",
    "get_tmux_manager_for_context",
    "LocalAgentRunManager",
    "register_terminal_tools",
]


def _resolve_tmux_target(
    session_id: str,
    session_manager: SessionManager,
    agent_run_manager: LocalAgentRunManager,
) -> tuple[str | None, TmuxSessionManager | None, str | None]:
    """Resolve a session ID to a tmux target through this module's patchable facade."""
    return _resolve_tmux_target_impl(
        session_id,
        session_manager,
        agent_run_manager,
        tmux_manager_factory=get_tmux_manager_for_context,
    )


def _authorize_send_keys_target(
    session_ref: str,
    session_manager: SessionManager,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve a send_keys target and verify it is within the caller's scope."""
    from gobby.utils.session_context import get_current_session_id

    caller_ref = get_current_session_id()
    if not caller_ref:
        return None, {
            "success": False,
            "error": "send_keys requires current MCP SessionContext",
            "error_code": "send_keys_caller_required",
        }

    try:
        caller_id = session_manager.resolve_session_reference(caller_ref)
    except ValueError as exc:
        return None, {
            "success": False,
            "error": f"Could not resolve send_keys caller: {exc}",
            "error_code": "send_keys_caller_not_found",
        }

    caller = session_manager.get(caller_id)
    if caller is None:
        return None, {
            "success": False,
            "error": f"Send_keys caller session {caller_id} not found",
            "error_code": "send_keys_caller_not_found",
        }

    if caller.agent_run_id:
        return None, {
            "success": False,
            "error": "Autonomous agent sessions cannot use send_keys",
            "error_code": "send_keys_autonomous_agent_forbidden",
            "caller_session_id": caller_id,
        }

    try:
        target_id = session_manager.resolve_session_reference(session_ref, caller.project_id)
    except ValueError as exc:
        return None, {
            "success": False,
            "error": str(exc),
            "error_code": "send_keys_target_not_found",
        }

    target = session_manager.get(target_id)
    if target is None:
        return None, {
            "success": False,
            "error": f"Session {session_ref} not found",
            "error_code": "send_keys_target_not_found",
        }

    if (
        target_id == caller_id
        or target.project_id == caller.project_id
        or session_manager.is_ancestor(caller_id, target_id)
        or session_manager.is_ancestor(target_id, caller_id)
    ):
        return target_id, None

    return None, {
        "success": False,
        "error": "send_keys target is outside the caller's project and agent tree",
        "error_code": "send_keys_target_forbidden",
        "caller_session_id": caller_id,
        "target_session_id": target_id,
    }


async def _send_terminal_compaction_command(
    tmux: TmuxSessionManager,
    target: str,
    command: str,
    session_id: str,
    *,
    cli_source: str | None,
    mark_continuation_pending: Callable[[], bool],
    clear_continuation_pending: Callable[[], bool],
    schedule_continuation_readiness: Callable[[str | None], bool] | None = None,
    continuation_readiness_capture_lines: int | None = None,
    observe_codex_interrupt: Callable[[], bool | None] | None = None,
    settle_seconds: float | None = None,
) -> tuple[bool, str | None, bool, dict[str, Any] | None]:
    """Persist continuation state, confirm interruption, then compact."""
    return await _send_terminal_compaction_command_impl(
        tmux,
        target,
        command,
        session_id,
        cli_source=cli_source,
        mark_continuation_pending=mark_continuation_pending,
        clear_continuation_pending=clear_continuation_pending,
        schedule_continuation_readiness=schedule_continuation_readiness,
        continuation_readiness_capture_lines=continuation_readiness_capture_lines,
        observe_codex_interrupt=observe_codex_interrupt,
        settle_seconds=settle_seconds,
        interrupt_settle_seconds=(
            _CODEX_INTERRUPT_SETTLE_SECONDS
            if cli_source == "codex"
            else _DEFAULT_INTERRUPT_SETTLE_SECONDS
        ),
        rejection_settle_seconds=_COMPACTION_REJECTION_SETTLE_SECONDS,
    )


def _resolve_session_for_compaction(
    session_id: str,
    session_manager: SessionManager,
) -> tuple[str | None, Any | None, str | None]:
    """Resolve a user-facing session ref for compact_self."""
    resolved_id = session_id
    resolver = getattr(session_manager, "resolve_session_reference", None)
    if callable(resolver):
        try:
            from gobby.utils.project_context import get_project_context

            project_ctx = get_project_context()
            project_id = project_ctx.get("id") if project_ctx else None
            candidate = resolver(session_id, project_id)
            if isinstance(candidate, str) and candidate:
                resolved_id = candidate
        except ValueError as exc:
            return None, None, f"Session {session_id} not found: {exc}"
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            logger.warning(
                "Failed resolving session reference %r for compaction: %s",
                session_id,
                detail,
                exc_info=True,
            )
            return None, None, f"failed to resolve session {session_id}: {detail}"

    session = session_manager.get(resolved_id)
    if session is None:
        return resolved_id, None, f"Session {session_id} not found"
    return resolved_id, session, None


def _backfill_tmux_context_from_sibling(
    session_id: str,
    session: Any,
    session_manager: SessionManager,
) -> Any | None:
    """Copy tmux context from a same-identity terminal sibling into session_id."""
    external_id = getattr(session, "external_id", None)
    machine_id = getattr(session, "machine_id", None)
    project_id = getattr(session, "project_id", None)
    if not all(isinstance(value, str) and value for value in (external_id, machine_id, project_id)):
        return None

    finder = getattr(session_manager, "find_by_external_id_all_sources", None)
    if not callable(finder):
        return None

    try:
        candidates = finder(
            external_id=external_id,
            machine_id=machine_id,
            project_id=project_id,
            session_type="terminal",
        )
    except Exception as exc:
        logger.debug(
            "Failed finding sibling terminal sessions for compact_self session %s: %s",
            session_id,
            exc,
            exc_info=True,
        )
        return None

    for candidate in candidates or ():
        if getattr(candidate, "id", None) == session_id:
            continue
        if getattr(candidate, "session_type", "terminal") != "terminal":
            continue
        if (
            getattr(candidate, "external_id", None) != external_id
            or getattr(candidate, "machine_id", None) != machine_id
            or getattr(candidate, "project_id", None) != project_id
        ):
            continue

        sibling_context = parse_terminal_context_value(getattr(candidate, "terminal_context", None))
        if not terminal_context_has_tmux_target(sibling_context):
            continue

        try:
            updated_session, _tmux_target_added = session_manager.backfill_terminal_context(
                session_id,
                sibling_context,
            )
        except Exception as exc:
            logger.debug(
                "Failed backfilling tmux context into compact_self session %s: %s",
                session_id,
                exc,
                exc_info=True,
            )
            return None
        return updated_session or session_manager.get(session_id)

    return None


def register_terminal_tools(
    registry: InternalToolRegistry,
    session_manager: SessionManager,
    db: HubDatabase,
    llm_service_resolver: Callable[[], Any | None] | None = None,
    memory_manager_resolver: Callable[[], Any | None] | None = None,
    session_summary_config: SessionSummaryConfig | None = None,
    compact_handoff_config: CompactHandoffConfig | None = None,
    config_resolver: Callable[[], DaemonConfig | None] | None = None,
    web_chat_session_registry: WebChatSessionRegistry | None = None,
) -> None:
    """Register send_keys, capture_output, compact_self, and clear_self tools."""

    agent_run_manager = LocalAgentRunManager(db)
    from gobby.mcp_proxy.tools.sessions._terminal_clear import register_clear_self_tool

    register_clear_self_tool(
        registry,
        session_manager=session_manager,
        db=db,
        agent_run_manager=agent_run_manager,
        web_chat_session_registry=web_chat_session_registry,
    )

    @registry.tool(
        name="send_keys",
        description=(
            "Send keystrokes to a session's tmux terminal. "
            "This is for terminal control; use `gobby-agents:send_message` for direct "
            "cross-session agent communication. "
            "Autonomous agent-run sessions cannot use this tool. "
            "Targets must be the caller, in the same project, or in the same agent tree. "
            "Use literal=true (default) to paste text — one or more trailing \\n characters "
            "produce exactly one Enter after the literal paste settles. "
            "Use literal=false for tmux key names: C-c, Escape, Enter, C-d."
        ),
    )
    async def send_keys(
        session_id: str,
        keys: str,
        literal: bool = True,
    ) -> dict[str, Any]:
        resolved_session_id, authorization_error = _authorize_send_keys_target(
            session_id,
            session_manager,
        )
        if authorization_error is not None:
            return authorization_error

        assert resolved_session_id is not None
        target, tmux, error = _resolve_tmux_target(
            resolved_session_id,
            session_manager,
            agent_run_manager,
        )
        if error:
            return {"success": False, "error": error}

        assert target is not None
        assert tmux is not None
        ok = await tmux.send_keys(target, keys, literal=literal)
        if not ok:
            return {
                "success": False,
                "error": f"tmux send-keys failed for session {session_id}",
            }
        return {"success": True}

    @registry.tool(
        name="compact_self",
        description=(
            "Compact your own context. In a terminal session this call does not "
            "return normally: the Gobby daemon interrupts your active turn and "
            "types the provider-specific compaction command into your pane, so "
            "your CLI reports the call as rejected or cancelled and attributes "
            "it to the user. That attribution is wrong — the daemon did it, and "
            "the compaction is proceeding. Do not stop, and do not ask the user "
            "what happened; a continuation prompt carrying your handoff summary "
            "arrives automatically once compaction finishes. For Codex, "
            "`Error: interrupted` and `Conversation interrupted` are likewise "
            "expected when followed by `Context compacted`. Web-chat sessions "
            "compact through the live daemon ChatSession registry and do return "
            "normally. Designed to be called at workflow handoff boundaries — "
            "e.g. /gobby plan calls this after spawning plan-adversary so the "
            "coordinator's bulky requirements-gathering context is summarized "
            "away while the sub-agent runs."
        ),
    )
    async def compact_self(rule_name: str | None = None) -> dict[str, Any]:
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return {
                "compacted": False,
                "reason": "compact_self requires current MCP SessionContext",
            }
        if rule_name:
            logger.info("Compacting session %s (triggered by rule %s)", session_id, rule_name)
        resolved_session_id, session, error = _resolve_session_for_compaction(
            session_id,
            session_manager,
        )
        if error:
            web_chat_fallback = await _compact_live_web_chat_fallback(
                web_chat_session_registry,
                session_id,
                resolved_session_id,
            )
            if web_chat_fallback is not None:
                return web_chat_fallback
            return {"compacted": False, "reason": error}
        assert resolved_session_id is not None
        assert session is not None

        session_type = getattr(session, "session_type", "terminal")
        source = getattr(session, "source", None)

        if session_type == "web_chat":
            if web_chat_session_registry is None:
                return {
                    "compacted": False,
                    "reason": "web_chat session registry is not available",
                }
            if (
                resolved_session_id != session_id
                and web_chat_session_registry.find_session(resolved_session_id)[1] is None
            ):
                return await web_chat_session_registry.compact_session(session_id)
            return await web_chat_session_registry.compact_session(resolved_session_id)

        if session_type != "terminal":
            return {
                "compacted": False,
                "reason": f"unsupported session_type: {session_type}",
            }
        if getattr(session, "status", None) == "deleted":
            return {
                "compacted": False,
                "reason": f"Session {resolved_session_id} is deleted",
                "error_code": "session_deleted",
            }

        command = _CLI_COMPACT_COMMANDS.get(source) if source else None
        if command is None:
            return {
                "compacted": False,
                "reason": f"no compaction command known for cli={source!r}",
            }

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
            return {"compacted": False, "reason": error}
        assert target is not None
        assert tmux is not None

        try:
            pane_probe = await tmux.capture_pane(target, lines=1)
        except Exception as exc:
            logger.warning(
                "Failed verifying compact_self tmux target %s for session %s",
                target,
                resolved_session_id,
                extra={
                    "event": "compact_self_tmux_target_verification_failed",
                    "session_id": resolved_session_id,
                    "tmux_target": target,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                exc_info=True,
            )
            return {
                "compacted": False,
                "reason": f"failed to verify live tmux target {target}: {exc}",
                "error_code": "tmux_target_verification_failed",
            }
        if pane_probe is None:
            return {
                "compacted": False,
                "reason": f"tmux target {target} is not live",
                "error_code": "tmux_target_not_live",
            }

        if getattr(session, "status", None) == "expired":
            activity = reconcile_compact_session_activity(
                session_manager,
                resolved_session_id,
            )
            if not activity.success:
                detail = activity.error_result()
                return {
                    "compacted": False,
                    "reason": f"{detail['error_code']}: {detail['error']}",
                    **detail,
                }
            session = activity.session
            assert session is not None

        codex_rollout_cursor: CodexRolloutCursor | None = None
        if source == "codex":
            try:
                codex_rollout_cursor = CodexRolloutCursor.at_eof(
                    getattr(session, "transcript_path", None)
                )
            except CodexRolloutObservationError as exc:
                logger.warning(
                    "Cannot observe Codex interruption for compact_self session %s: %s",
                    resolved_session_id,
                    exc,
                )
                return {
                    "compacted": False,
                    "continuation_pending": False,
                    "reason": str(exc),
                    "error_code": "codex_interrupt_observation_unavailable",
                }

        operation_config = config_resolver() if config_resolver is not None else None
        operation_session_summary_config, operation_compact_handoff_config = (
            _capture_handoff_configs(
                None,
                session_summary_config=(
                    operation_config.session_summary
                    if operation_config is not None
                    else session_summary_config
                ),
                compact_handoff_config=(
                    operation_config.compact_handoff
                    if operation_config is not None
                    else compact_handoff_config
                ),
            )
        )
        memory_manager = memory_manager_resolver() if memory_manager_resolver is not None else None
        refresh_result = await _refresh_compact_handoff_context(
            resolved_session_id,
            session,
            session_manager,
            db,
            llm_service_resolver() if llm_service_resolver is not None else None,
            operation_session_summary_config,
            memory_manager=memory_manager,
            config=operation_config,
            compact_handoff_config=operation_compact_handoff_config,
        )
        if not refresh_result.get("success"):
            return {
                "compacted": False,
                "reason": "handoff context refresh failed before compaction: "
                f"{refresh_result.get('error', 'unknown error')}",
            }

        resume_skills = persist_compact_resume_required_skills(db, resolved_session_id)
        continuation_prompt = build_compact_self_continue_prompt(
            summary_session_id=resolved_session_id,
        )
        compact_attempt_id = uuid4().hex if source == "codex" else None
        observe_codex_interrupt: Callable[[], bool | None] | None = None
        if codex_rollout_cursor is not None:

            def observe_codex_rollout_interrupt() -> bool | None:
                try:
                    return codex_rollout_cursor.saw_fresh_turn_aborted()
                except CodexRolloutObservationError as exc:
                    logger.warning(
                        "Lost Codex interrupt observation for compact_self session %s: %s",
                        resolved_session_id,
                        exc,
                    )
                    return None

            observe_codex_interrupt = observe_codex_rollout_interrupt
        schedule_continuation_readiness: Callable[[str | None], bool] | None = None
        if source == "codex":

            def schedule_codex_readiness(before_command: str | None) -> bool:
                return schedule_codex_compact_self_continuation_readiness(
                    db,
                    pending_session_id=resolved_session_id,
                    target_session=session,
                    before_command=before_command,
                    attempt_id=compact_attempt_id,
                )

            schedule_continuation_readiness = schedule_codex_readiness
        ok, reason, continuation_pending, failure_detail = await _send_terminal_compaction_command(
            tmux,
            target,
            command,
            resolved_session_id,
            cli_source=source,
            mark_continuation_pending=lambda: mark_compact_self_continuation_pending(
                db,
                resolved_session_id,
                prompt=continuation_prompt,
                summary_session_id=resolved_session_id,
                attempt_id=compact_attempt_id,
            ),
            clear_continuation_pending=lambda: clear_compact_self_continuation_pending(
                db,
                resolved_session_id,
                attempt_id=compact_attempt_id,
            ),
            schedule_continuation_readiness=schedule_continuation_readiness,
            continuation_readiness_capture_lines=(
                CODEX_COMPACT_READY_CAPTURE_LINES if source == "codex" else None
            ),
            observe_codex_interrupt=observe_codex_interrupt,
        )

        if not ok:
            if source == "codex":
                try:
                    session_manager.update_status(resolved_session_id, "active")
                except Exception:
                    logger.warning(
                        "Failed restoring session %s to active after Codex compaction failure",
                        resolved_session_id,
                        exc_info=True,
                    )
            failure_result = {
                "compacted": False,
                "reason": reason,
            }
            if failure_detail is not None:
                failure_result.update(failure_detail)
            return failure_result
        background_refresh_scheduled = False
        if refresh_result.get("background_refresh_needed"):
            background_refresh_scheduled = _schedule_compact_handoff_background_refresh(
                resolved_session_id,
                session_manager,
                db,
                llm_service_resolver() if llm_service_resolver is not None else None,
                operation_session_summary_config,
                operation_compact_handoff_config,
                memory_manager=memory_manager,
                config=operation_config,
            )
        result = {
            "compacted": True,
            "command": command,
            "cli": source,
            "via": "tmux",
            "interrupted": True,
            "continuation_pending": continuation_pending,
        }
        if any(resume_skills.values()):
            result["compact_resume_required_skills"] = resume_skills
        if refresh_result.get("refreshed"):
            result["handoff_context_refreshed"] = True
            result["handoff_summary_length"] = refresh_result.get("summary_length")
        if refresh_result.get("fallback"):
            result["handoff_context_fallback"] = True
        if refresh_result.get("timed_out"):
            result["handoff_context_refresh_timed_out"] = True
        if background_refresh_scheduled:
            result["handoff_context_background_refresh_scheduled"] = True
        return result

    @registry.tool(
        name="capture_output",
        description=(
            "Capture the last N lines of a session's tmux terminal output. "
            "Useful for inspecting permission dialogs, trust prompts, or "
            "other terminal state not visible through hooks."
        ),
    )
    async def capture_output(
        session_id: str,
        lines: int = 50,
    ) -> dict[str, Any]:
        target, tmux, error = _resolve_tmux_target(session_id, session_manager, agent_run_manager)
        if error:
            fallback, transcript_error = await _capture_transcript_tail(
                session_id,
                session_manager,
                lines,
                tmux_error=error,
            )
            if fallback is not None:
                return fallback
            return {
                "success": False,
                "error": error,
                "error_code": "no_live_pane_or_transcript",
                "tmux_error": error,
                "transcript_error": transcript_error,
            }

        assert target is not None
        assert tmux is not None
        output = await tmux.capture_pane(target, lines)
        if output is None:
            return {
                "success": False,
                "error": f"Failed to capture pane for session {session_id}",
            }
        return {"success": True, "output": output, "via": "tmux"}
