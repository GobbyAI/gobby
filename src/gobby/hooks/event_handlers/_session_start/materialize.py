"""Session row activation after SessionStart identity resolution."""

from __future__ import annotations

import json
from typing import Any

import psycopg

from gobby.hooks.events import HookEvent
from gobby.hooks.terminal_context import (
    enrich_terminal_context_with_cwd,
    hook_cwd,
    is_gobby_acp_child,
)
from gobby.sessions.clear_continuation import (
    schedule_handoff_continuation,
    take_clear_handoff_marker,
)
from gobby.sessions.handoff import build_handoff_continue_prompt

from .agents import _seed_parent_turn_seq, _seed_wiki_overview_var
from .claims import preserve_task_claim_state
from .context import classify_session_start_context
from .handoff import (
    STARTUP_SOURCES,
    SessionStartResolution,
    prepare_compact_continuation_variables,
)
from .profile import seed_user_profile_content
from .terminal_runtime import (
    expire_stale_terminal_sessions_for_context,
    session_start_is_nested_cli_child,
)
from .transcripts import replace_session_message_processor

_CONTEXT_MODE_METADATA_KEY = "_session_start_context_mode"


def _compat_module() -> Any:
    import gobby.hooks.event_handlers._session_start as session_start

    return session_start


def session_start_should_defer(
    event: HookEvent,
    existing_session: Any | None,
    session_source: str | None,
) -> bool:
    """Return whether this startup SessionStart may defer row creation."""
    session_source = session_source or "startup"
    if session_source not in STARTUP_SOURCES:
        return False
    if existing_session is not None:
        # An expired row is re-activated through the register path, as before;
        # deferring it would hand the row to lookup recovery with no activation.
        return False

    raw_terminal_context = event.data.get("terminal_context")
    terminal_context = raw_terminal_context if isinstance(raw_terminal_context, dict) else None
    terminal_context = enrich_terminal_context_with_cwd(
        terminal_context,
        hook_cwd(event.data, event.cwd),
    )
    if is_gobby_acp_child(terminal_context):
        return False
    return not session_start_is_nested_cli_child(event.source.value, terminal_context)


def _consume_pending_handoff_compact_continuation(
    handler: Any,
    *,
    session_source: str,
    pending_session_id: str | None,
    target_session: Any,
) -> bool:
    """Consume provider compact markers after a confirmed compact restart."""
    if session_source != "compact" or not handler._session_manager:
        return False
    return bool(
        _compat_module().consume_and_schedule_handoff_compact_continuation(
            handler._session_manager.db,
            pending_session_id=pending_session_id,
            target_session=target_session,
            loop=getattr(handler._session_coordinator, "_event_loop", None),
        )
    )


def _schedule_tmux_window_rename_for_session(handler: Any, session: Any) -> None:
    terminal_context = getattr(session, "terminal_context", None)
    pane = terminal_context.get("tmux_pane") if isinstance(terminal_context, dict) else None
    if not pane:
        handler.logger.debug(
            "tmux window rename skipped for %s: no tmux_pane in terminal_context",
            getattr(session, "ref", "?"),
        )
        return

    title = getattr(session, "title", None) or ""
    handler.logger.debug(
        "Scheduling tmux window rename for %s pane=%s",
        getattr(session, "ref", "?"),
        pane,
    )
    handler.logger.debug(
        "Scheduling tmux window rename title for %s: %r",
        getattr(session, "ref", "?"),
        title,
    )
    _compat_module().schedule_tmux_window_rename(
        session,
        title,
        loop=getattr(handler._session_coordinator, "_event_loop", None),
    )


def _reset_agent_context_injection(handler: Any, session_id: str | None) -> None:
    """Force the next before_agent hook to rehydrate prompt-facing agent context."""
    if not session_id or not handler._session_manager:
        return
    try:
        from gobby.workflows.state_manager import SessionVariableManager

        SessionVariableManager(handler._session_manager.db).merge_variables(
            session_id,
            {
                "_agent_context_injected": False,
                "_agent_context_rehydrate_pending": True,
                "wiki_overview_injected": False,
            },
        )
    except (json.JSONDecodeError, KeyError, psycopg.Error) as exc:
        handler.logger.warning("Failed to reset agent context injection flag: %s", exc)


def _bind_clear_successor(
    handler: Any,
    resolution: SessionStartResolution | None,
    session_obj: Any,
) -> bool:
    """Take the clear marker and apply isolated successor side effects."""
    predecessor = getattr(resolution, "clear_predecessor", None)
    attempt_id = getattr(resolution, "clear_attempt_id", None)
    if predecessor is None or not attempt_id or handler._session_manager is None:
        return False
    predecessor_id = getattr(predecessor, "id", None)
    successor_id = getattr(session_obj, "id", None)
    if not isinstance(predecessor_id, str) or not isinstance(successor_id, str):
        return False
    won = take_clear_handoff_marker(
        handler._session_manager.db,
        predecessor_id,
        attempt_id=attempt_id,
        successor_id=successor_id,
    )
    if not won:
        handler.logger.warning(
            "Clear handoff take lost for predecessor %s successor %s",
            predecessor_id,
            successor_id,
            extra={
                "event": "clear_handoff_take_lost",
                "predecessor_id": predecessor_id,
                "successor_id": successor_id,
                "attempt_id": attempt_id,
            },
        )
        return False
    try:
        predecessor_vars: dict[str, Any] = {}
        sv_mgr: Any | None = None
        try:
            from gobby.workflows.state_manager import SessionVariableManager

            sv_mgr = SessionVariableManager(handler._session_manager.db)
            predecessor_vars = dict(sv_mgr.get_variables(predecessor_id) or {})
        except Exception:
            predecessor_vars = {}
        preserve_task_claim_state(
            handler,
            sv_mgr,
            successor_id,
            predecessor_id,
            predecessor_vars,
        )
    except Exception as exc:
        handler.logger.warning(
            "Failed to reassign clear task claims for successor %s: %s",
            successor_id,
            exc,
        )
    try:
        from gobby.sessions.title_lifecycle import apply_clear_successor_title

        apply_clear_successor_title(handler._session_manager, successor_id, predecessor)
    except Exception as exc:
        handler.logger.warning(
            "Failed to set clear-successor title for session %s: %s",
            successor_id,
            exc,
        )
    return True


def _schedule_clear_continuation(handler: Any, session_obj: Any, successor_id: str) -> None:
    """Type the get_handoff pull prompt into the successor pane."""
    try:
        prompt = build_handoff_continue_prompt()
        schedule_handoff_continuation(
            session_obj,
            prompt,
            loop=getattr(handler._session_coordinator, "_event_loop", None),
        )
    except Exception as exc:
        handler.logger.warning(
            "Failed to schedule clear continuation for successor %s: %s",
            successor_id,
            exc,
        )


def activate_materialized_session(
    handler: Any,
    event: HookEvent,
    session_id: str,
    *,
    resolution: SessionStartResolution | None = None,
    session_obj: Any | None = None,
    project_id: str | None = None,
    transcript_path: str | None = None,
    terminal_context: dict[str, Any] | None = None,
) -> list[str]:
    """Activate agents, seeds, code index, and transcript processing.

    This is the behavior-preserving SessionStart activation body. The return
    value is the additional context passed to session response composition.
    """
    input_data = event.data
    cli_source = event.source.value
    external_id = str(event.session_id or "").strip()
    session_source = str(input_data.get("source", "startup"))
    workflow_name = input_data.get("workflow_name")
    parent_session_id = input_data.get("parent_session_id")

    if terminal_context is None:
        raw_terminal_context = input_data.get("terminal_context")
        terminal_context = raw_terminal_context if isinstance(raw_terminal_context, dict) else None
        terminal_context = enrich_terminal_context_with_cwd(
            terminal_context,
            hook_cwd(input_data, event.cwd),
        )

    if session_obj is None and handler._session_manager:
        session_obj = handler._session_manager.get(session_id)
    project_id = project_id or event.project_id or getattr(session_obj, "project_id", None)
    if transcript_path is None:
        transcript_path = input_data.get("transcript_path") or getattr(
            session_obj,
            "transcript_path",
            None,
        )

    expire_stale_terminal_sessions_for_context(
        handler,
        session_id=session_id,
        project_id=project_id,
        terminal_context=terminal_context,
    )
    if (
        handler.terminal_manager is not None
        and isinstance(terminal_context, dict)
        and isinstance(project_id, str)
        and session_id
    ):
        from gobby.storage.terminals import ProjectOwnershipConflictError
        from gobby.terminals.discovery import seed_external_terminal

        try:
            seed_external_terminal(
                handler.terminal_manager,
                project_id=project_id,
                session_id=session_id,
                terminal_context=terminal_context,
            )
        except ProjectOwnershipConflictError:
            handler.logger.info("external terminal discovery conflict for session %s", session_id)

    handler._setup_code_index(session_id, project_id)

    if workflow_name:
        handler.logger.debug(
            "Pipeline workflow registered for session -- agent will execute via run_pipeline",
            extra={"workflow_name": workflow_name, "session_id": session_id},
        )

    if handler._session_manager is not None:
        try:
            _seed_parent_turn_seq(handler, session_id)
        except Exception as exc:
            handler.logger.warning("Failed to seed memory recall vars: %s", exc)
        _seed_wiki_overview_var(handler, session_id, project_id)

    clear_predecessor = getattr(resolution, "clear_predecessor", None)
    if session_obj is not None and (session_source == "clear" or clear_predecessor is not None):
        bound = _bind_clear_successor(handler, resolution, session_obj)
        if bound and session_source == "clear":
            successor_id = getattr(session_obj, "id", None)
            if isinstance(successor_id, str):
                _schedule_clear_continuation(handler, session_obj, successor_id)
        if handler._session_manager is not None:
            rebound = handler._session_manager.get(session_id)
            if rebound is not None:
                session_obj = rebound
    if session_obj:
        _schedule_tmux_window_rename_for_session(handler, session_obj)

    context_decision = classify_session_start_context(
        handler,
        session_id=session_id,
        session=session_obj,
        session_source=session_source,
        is_existing_session=False,
    )
    event.metadata[_CONTEXT_MODE_METADATA_KEY] = context_decision.mode

    if not input_data.get("skip_default_agent_activation"):
        try:
            agent_override = input_data.get("agent_name_override")
            handler._activate_default_agent(
                session_id,
                cli_source,
                project_id,
                agent_name_override=agent_override,
            )
        except Exception as exc:
            handler.logger.exception("Failed to activate default agent: %s", exc)

    if handler._session_manager is not None:
        try:
            seed_user_profile_content(handler, session_id)
        except (KeyError, json.JSONDecodeError, psycopg.Error) as exc:
            handler.logger.warning("Failed to seed user profile vars: %s", exc)

    if transcript_path and handler._session_coordinator:
        try:
            handler._session_coordinator.register_session(external_id)
        except Exception as exc:
            handler.logger.exception("Failed to setup session tracking: %s", exc)

    effective_parent_session_id = parent_session_id or getattr(
        session_obj,
        "parent_session_id",
        None,
    )
    event.metadata["_platform_session_id"] = session_id
    if effective_parent_session_id:
        event.metadata["_parent_session_id"] = effective_parent_session_id

    message_processor = handler._resolve_message_processor()
    if message_processor is not None and transcript_path:
        try:
            replace_session_message_processor(
                handler,
                session_id,
                message_processor,
                transcript_path,
                source=cli_source,
            )
        except Exception as exc:
            handler.logger.warning("Failed to register session with message processor: %s", exc)

    additional_context: list[str] = []
    if context_decision.mode == "full":
        _reset_agent_context_injection(handler, session_id)

    try:
        prepare_compact_continuation_variables(handler, session_id, session_source)
    except (KeyError, json.JSONDecodeError, psycopg.Error) as exc:
        handler.logger.warning("Failed to prepare compact continuation vars: %s", exc)

    if project_id and not event.task_id:
        claimed_ctx = handler._build_claimed_task_context(
            session_id,
            project_id,
            compact=context_decision.mode == "live",
        )
        if claimed_ctx:
            additional_context.append(claimed_ctx)

    if event.task_id and handler._session_manager:
        task_title = event.metadata.get("_task_title", "Unknown Task")
        task_context_str = f"You are working on task: {task_title} ({event.task_id})"
        try:
            from gobby.workflows.state_manager import SessionVariableManager

            SessionVariableManager(handler._session_manager.db).merge_variables(
                session_id,
                {"task_context": task_context_str},
            )
        except (KeyError, json.JSONDecodeError, psycopg.Error) as exc:
            handler.logger.warning("Failed to persist task context: %s", exc)

    if event.task_id:
        task_title = event.metadata.get("_task_title", "Unknown Task")
        additional_context.append("\n## Active Task Context\n")
        additional_context.append(f"You are working on task: {task_title} ({event.task_id})")

    if session_obj:
        _consume_pending_handoff_compact_continuation(
            handler,
            session_source=session_source,
            pending_session_id=session_id,
            target_session=session_obj,
        )

    return additional_context
