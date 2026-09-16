"""Terminal interaction tools for tmux-backed sessions.

Exposes send_keys, capture_output, and structured handoff tools on gobby-sessions,
enabling orchestration (heartbeat, pipelines, other agents)
to interact with running terminal sessions.
"""

from __future__ import annotations

import asyncio as asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from gobby.agents.provider_capabilities import provider_capabilities
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.hooks.grok_pending_context import clear_queued_context
from gobby.mcp_proxy.tools.sessions._terminal_send_keys import (
    _authorize_send_keys_target as _authorize_send_keys_target,
)
from gobby.mcp_proxy.tools.sessions._terminal_send_keys import (
    register_send_keys_tool,
)
from gobby.mcp_proxy.tools.sessions._terminal_tmux import (
    _CLI_COMPACT_COMMANDS,
    _CLI_COMPACT_INTERRUPT_KEYS,
    _COMPACTION_REJECTION_CAPTURE_LINES,
    _COMPACTION_REJECTION_ERROR_CODE,
    _COMPACTION_REJECTION_SETTLE_SECONDS,
    _DEFAULT_COMPACT_INTERRUPT_KEY,
    _DEFAULT_INTERRUPT_SETTLE_SECONDS,
    _INTERRUPT_OBSERVATION_UNAVAILABLE_ERROR_CODE,
    _OBSERVED_INTERRUPT_SETTLE_SECONDS,
    _capture_pane_snapshot,
    _compact_interrupt_key,
    _detect_compaction_rejection,
    _fresh_output_delta,
    _send_pane_key,
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
from gobby.prompts.loader import PromptLoader
from gobby.sessions.handoff import (
    restore_handoff_attempt,
    stage_handoff_attempt,
    staged_handoff_tool_result,
)
from gobby.sessions.handoff_records import (
    HandoffPayload,
    build_handoff_payload,
    record_handoff_delivery,
)
from gobby.sessions.transcript_cursor import (
    TranscriptObservationError,
    build_interrupt_observer,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_activity import reconcile_compact_session_activity
from gobby.terminal_context import parse_terminal_context_value, terminal_context_has_tmux_target
from gobby.terminals.lookup import manager_for_terminal_context
from gobby.terminals.pane_io import PaneIO, TmuxPaneIO, live_runtime_pane
from gobby.workflows.session_feedback_survey import survey_is_active
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)

# A spawned run whose provider reads nothing from its terminal -- Grok ``--single``
# (#22364) and ``droid exec`` (#22402): typed commands never arrive, so the pane keeps
# them unconsumed. For Grok the interrupt key is also a plain SIGINT that kills the run.
_HEADLESS_AGENT_RUN_ERROR_CODE = "headless_agent_run"
_HEADLESS_AGENT_RUN_GUIDANCE = (
    "Do not call set_handoff again in this run. Continue the task with the remaining "
    "context, keep tool results small, and finish with end_agent_run."
)

__all__ = [
    "_CLI_COMPACT_COMMANDS",
    "_CLI_COMPACT_INTERRUPT_KEYS",
    "_COMPACTION_REJECTION_CAPTURE_LINES",
    "_COMPACTION_REJECTION_ERROR_CODE",
    "_COMPACTION_REJECTION_SETTLE_SECONDS",
    "_DEFAULT_COMPACT_INTERRUPT_KEY",
    "_INTERRUPT_OBSERVATION_UNAVAILABLE_ERROR_CODE",
    "_OBSERVED_INTERRUPT_SETTLE_SECONDS",
    "_TRANSCRIPT_TAIL_MAX_BYTES",
    "_capture_pane_snapshot",
    "_capture_transcript_tail",
    "_compact_interrupt_key",
    "_detect_compaction_rejection",
    "_fresh_output_delta",
    "_interrupt_observer",
    "_read_transcript_tail_lines",
    "_resolve_pane_io",
    "_resolve_session_for_compaction",
    "_authorize_send_keys_target",
    "_resolve_tmux_target",
    "_send_pane_key",
    "_send_terminal_compaction_command",
    "asyncio",
    "manager_for_terminal_context",
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
        tmux_manager_factory=manager_for_terminal_context,
    )


def _resolve_pane_io(
    session_id: str,
    session_manager: SessionManager,
    agent_run_manager: LocalAgentRunManager,
    *,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> tuple[PaneIO | None, str | None]:
    """Route through the session's live terminals row when one exists, else raw tmux."""
    pane = live_runtime_pane(session_id, terminal_manager, terminal_runtime_registry)
    if pane is not None:
        return pane, None
    target, tmux, error = _resolve_tmux_target(session_id, session_manager, agent_run_manager)
    if error:
        return None, error
    assert target is not None
    assert tmux is not None
    return TmuxPaneIO(tmux, target), None


def _interrupt_observer(
    source: Any,
    session: Any,
) -> tuple[Callable[[], bool | None] | None, str | None]:
    """Build the transcript interrupt observer; ``(None, error)`` fails closed."""
    try:
        observer = build_interrupt_observer(
            source if isinstance(source, str) else None,
            getattr(session, "transcript_path", None),
            session_id=getattr(session, "id", None),
        )
    except TranscriptObservationError as exc:
        logger.warning(
            "Cannot observe %s interruption for handoff on session %s: %s",
            source,
            getattr(session, "id", None),
            exc,
        )
        return None, str(exc)
    return observer, None


async def _send_terminal_compaction_command(
    pane: PaneIO,
    command: str,
    session_id: str,
    *,
    cli_source: str | None,
    mark_continuation_pending: Callable[[], bool],
    clear_continuation_pending: Callable[[], bool],
    schedule_continuation_readiness: Callable[[str | None], bool] | None = None,
    continuation_readiness_capture_lines: int | None = None,
    observe_interrupt: Callable[[], bool | None] | None = None,
    turn_settled: Callable[[], bool | None] | None = None,
    settle_seconds: float | None = None,
    composer_read: Callable[[str | None], Any] | None = None,
) -> tuple[bool, str | None, bool, dict[str, Any] | None]:
    """Persist continuation state, confirm interruption, drain the composer, then compact."""
    return await _send_terminal_compaction_command_impl(
        pane,
        command,
        session_id,
        cli_source=cli_source,
        mark_continuation_pending=mark_continuation_pending,
        clear_continuation_pending=clear_continuation_pending,
        schedule_continuation_readiness=schedule_continuation_readiness,
        continuation_readiness_capture_lines=continuation_readiness_capture_lines,
        observe_interrupt=observe_interrupt,
        turn_settled=turn_settled,
        settle_seconds=settle_seconds,
        interrupt_settle_seconds=(
            _OBSERVED_INTERRUPT_SETTLE_SECONDS
            if observe_interrupt is not None
            else _DEFAULT_INTERRUPT_SETTLE_SECONDS
        ),
        rejection_settle_seconds=_COMPACTION_REJECTION_SETTLE_SECONDS,
        composer_read=composer_read,
    )


def _resolve_session_for_compaction(
    session_id: str,
    session_manager: SessionManager,
) -> tuple[str | None, Any | None, str | None]:
    """Resolve a user-facing session ref for handoff compaction."""
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
            "Failed finding sibling terminal sessions for handoff compaction %s: %s",
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
                "Failed backfilling tmux context for handoff compaction %s: %s",
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
    web_chat_session_registry: WebChatSessionRegistry | None = None,
    terminal_manager: Any | None = None,
    terminal_runtime_registry: Any | None = None,
    write_coordinator: Any | None = None,
    task_manager: LocalTaskManager | None = None,
    config_resolver: Callable[[], Any | None] | None = None,
) -> None:
    """Register terminal control and structured handoff tools."""

    agent_run_manager = LocalAgentRunManager(db)
    register_send_keys_tool(
        registry,
        session_manager,
        db,
        terminal_manager=terminal_manager,
        write_coordinator=write_coordinator,
    )

    async def set_handoff(
        current_state: str,
        next_steps: list[str],
        what_was_accomplished: list[str] | None = None,
        key_decisions: list[str] | None = None,
        problems_encountered: list[str] | None = None,
        what_didnt_work: list[str] | None = None,
        blockers: list[str] | None = None,
        notes: list[str] | None = None,
        references: list[str] | None = None,
        clear_session: bool = False,
    ) -> dict[str, Any]:
        feedback_status = _require_handoff_prerequisites(clear_session=clear_session)
        if feedback_status.get("success") is False:
            return feedback_status

        try:
            handoff = build_handoff_payload(
                current_state=current_state,
                next_steps=next_steps,
                what_was_accomplished=what_was_accomplished or (),
                key_decisions=key_decisions or (),
                problems_encountered=problems_encountered or (),
                what_didnt_work=what_didnt_work or (),
                blockers=blockers or (),
                notes=notes or (),
                references=references or (),
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc), "error_code": "invalid_handoff"}

        if clear_session:
            from gobby.mcp_proxy.tools.sessions._terminal_clear import prepare_clear_session

            result = await prepare_clear_session(
                handoff,
                session_manager=session_manager,
                db=db,
                agent_run_manager=agent_run_manager,
                web_chat_session_registry=web_chat_session_registry,
                terminal_manager=terminal_manager,
                terminal_runtime_registry=terminal_runtime_registry,
            )
        else:
            result = await _compact_with_handoff(handoff)
        result.update(feedback_status)
        return result

    def _require_handoff_prerequisites(*, clear_session: bool) -> dict[str, Any]:
        from gobby.storage.tasks import LocalTaskManager
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return {
                "success": False,
                "error": "set_handoff requires current MCP SessionContext",
                "error_code": "session_context_required",
            }
        resolved_session_id, session, error = _resolve_session_for_compaction(
            session_id,
            session_manager,
        )
        if error:
            return {"success": False, "error": error, "error_code": "session_not_found"}
        assert resolved_session_id is not None
        assert session is not None

        variable_manager = SessionVariableManager(db)
        submitted = (
            variable_manager.get_variables(resolved_session_id).get(
                "_gobby_feedback_epoch_submitted"
            )
            is True
        )
        project_id = getattr(session, "project_id", None)
        project = LocalProjectManager(db).get(project_id) if isinstance(project_id, str) else None
        config = config_resolver() if config_resolver is not None else None
        feedback_config = getattr(config, "session_feedback", None)
        scope = getattr(feedback_config, "survey", "gobby")
        survey_active = survey_is_active(scope, project.name if project is not None else "")

        if survey_active and not submitted:
            return {
                "success": False,
                "error_code": "feedback_required",
                "error": (
                    "This project requires the bounded Gobby-experience survey before handoff. "
                    "Call gobby-sessions:feedback with at most 3 observations, or "
                    "observations=[] when there is nothing to report. Then call set_handoff last."
                ),
            }
        if clear_session and LocalTaskManager(db).list_tasks(
            claimed_by_session_id=resolved_session_id, closed=False, limit=1
        ):
            return {
                "success": False,
                "error_code": "active_task_requires_compact",
                "error": (
                    "Use clear_session=false while working inside a task. "
                    "clear_session=true is allowed only between tasks after task closure."
                ),
            }
        return {"feedback_submitted": submitted}

    async def _compact_with_handoff(
        handoff: HandoffPayload,
    ) -> dict[str, Any]:
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return {
                "compacted": False,
                "reason": "set_handoff requires current MCP SessionContext",
            }
        resolved_session_id, session, error = _resolve_session_for_compaction(
            session_id,
            session_manager,
        )
        if error:
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
                    "error_code": "web_chat_registry_unavailable",
                }
            compact_target = resolved_session_id
            if (
                resolved_session_id != session_id
                and web_chat_session_registry.find_session(resolved_session_id)[1] is None
            ):
                compact_target = session_id
            attempt_id = uuid4().hex
            attempt_state = None
            try:
                attempt_state = stage_handoff_attempt(
                    db,
                    resolved_session_id,
                    attempt_id=attempt_id,
                    handoff=handoff,
                    clear_session=False,
                )
                result = await web_chat_session_registry.compact_session(
                    compact_target,
                    handoff_attempt_id=attempt_id,
                )
            except Exception as exc:
                if attempt_state is not None:
                    restore_handoff_attempt(db, attempt_state)
                return {"compacted": False, "reason": str(exc), "error_code": "dispatch_failed"}
            if not result.get("compacted"):
                restore_handoff_attempt(db, attempt_state)
                return result
            clear_queued_context(session_manager, resolved_session_id)
            try:
                record_handoff_delivery(
                    db,
                    handoff_id=attempt_state.handoff_record_id,
                    attempt_id=attempt_id,
                    boundary_kind="compact",
                    continuation_session_id=resolved_session_id,
                )
                result["handoff_delivered"] = True
            except Exception:
                logger.warning(
                    "Failed recording compact handoff delivery %s for session %s",
                    attempt_id,
                    resolved_session_id,
                    exc_info=True,
                )
                result["handoff_delivered"] = False
            result["attempt_id"] = attempt_id
            result["handoff_staged"] = True
            return result

        if session_type != "terminal":
            return {
                "compacted": False,
                "reason": f"unsupported session_type: {session_type}",
                "error_code": "unsupported_session_type",
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
                "error_code": "no_compaction_command",
            }

        agent_run = agent_run_manager.get_by_session(resolved_session_id)
        provider = getattr(agent_run, "provider", None)
        if isinstance(provider, str) and provider_capabilities(provider).headless_spawn:
            return {
                "compacted": False,
                "reason": (
                    f"{provider} agent runs are headless: the CLI reads nothing from its "
                    "terminal, so no compaction command can be delivered"
                ),
                "error_code": _HEADLESS_AGENT_RUN_ERROR_CODE,
                "retry_guidance": _HEADLESS_AGENT_RUN_GUIDANCE,
            }

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
            return {
                "compacted": False,
                "reason": error,
                "error_code": "terminal_target_unavailable",
            }
        assert pane is not None

        if await pane.snapshot(1) is None:
            return {
                "compacted": False,
                "reason": f"{pane.backend} target {pane.target} is not live",
                "error_code": "terminal_target_not_live",
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

        observe_interrupt, observer_error = _interrupt_observer(source, session)
        if observer_error is not None:
            return {
                "compacted": False,
                "continuation_pending": False,
                "reason": observer_error,
                "error_code": _INTERRUPT_OBSERVATION_UNAVAILABLE_ERROR_CODE,
            }

        compact_attempt_id = uuid4().hex
        try:
            stage_handoff_attempt(
                db,
                resolved_session_id,
                attempt_id=compact_attempt_id,
                handoff=handoff,
                clear_session=False,
            )
        except Exception as exc:
            return {
                "compacted": False,
                "reason": f"failed to stage handoff: {exc}",
                "error_code": "staging_failed",
            }
        return staged_handoff_tool_result(
            attempt_id=compact_attempt_id,
            session_id=resolved_session_id,
            clear_session=False,
            command=command,
            cli=source,
            via=pane.backend,
        )

    try:
        handoff_description = PromptLoader(db=db).load("handoff/authoring").content
    except FileNotFoundError:
        # A registry can be constructed before bundled prompts have been synced.
        # Keep discovery available; authoring guidance belongs to the prompt row.
        handoff_description = "Store a structured handoff and compact or clear the current session."

    registry.register(
        name="set_handoff",
        description=handoff_description,
        brief="Store a structured handoff and compact or clear the current session.",
        input_schema={
            "type": "object",
            "properties": {
                "current_state": {"type": "string", "minLength": 1},
                "next_steps": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
                "what_was_accomplished": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "default": [],
                },
                "key_decisions": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "default": [],
                },
                "problems_encountered": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "default": [],
                },
                "what_didnt_work": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "default": [],
                },
                "blockers": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "default": [],
                },
                "notes": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "default": [],
                },
                "references": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "default": [],
                },
                "clear_session": {"type": "boolean", "default": False},
            },
            "required": ["current_state", "next_steps"],
            "additionalProperties": False,
        },
        func=set_handoff,
    )

    @registry.tool(
        name="capture_output",
        description=(
            "Take a one-shot diagnostic snapshot of the last N lines of a session's "
            "terminal output. Useful for inspecting permission dialogs, trust prompts, "
            "or other state not visible through hooks. For terminal-pattern waits, use "
            "gobby-agents:wait_for_output instead of repeated capture_output calls."
        ),
    )
    async def capture_output(
        session_id: str,
        lines: int = 50,
    ) -> dict[str, Any]:
        if terminal_manager is not None and terminal_runtime_registry is not None:
            terminal = terminal_manager.get_live_for_session(session_id)
            if terminal is not None:
                runtime = terminal_runtime_registry.resolve(terminal.backend)
                snapshot = await runtime.snapshot(terminal, lines)
                return {
                    "success": True,
                    "output": snapshot.text,
                    "via": terminal.backend,
                    "truncated": snapshot.truncated,
                    "dropped_bytes": snapshot.dropped_bytes,
                    "total_bytes": snapshot.total_bytes,
                }
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
        output = await tmux.snapshot_lines(target, lines)
        if output is None:
            return {
                "success": False,
                "error": f"Failed to capture pane for session {session_id}",
            }
        return {"success": True, "output": output, "via": "tmux"}
