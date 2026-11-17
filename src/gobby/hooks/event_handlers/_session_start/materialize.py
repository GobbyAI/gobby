"""Session row activation after SessionStart identity resolution."""

from __future__ import annotations

import json
from typing import Any

import psycopg

from gobby.hooks.events import ContextPart, HookEvent, HookEventType
from gobby.hooks.terminal_context import (
    enrich_terminal_context_with_cwd,
    hook_cwd,
    is_gobby_acp_child,
)
from gobby.storage.session_startup_marks import record_startup_mark

from .agents import _seed_parent_turn_seq
from .claims import (
    rehydrate_found_work_gate_arm,
)
from .context import (
    classify_session_start_context,
    stash_startup_claim,
)
from .context import (
    startup_claim_owner_token as _startup_claim_owner_token,
)
from .handoff import (
    STARTUP_SOURCES,
    SessionStartResolution,
    prepare_compact_continuation_variables,
)
from .profile import seed_user_profile_content
from .terminal_runtime import (
    discover_and_bind_external_terminal,
    expire_stale_terminal_sessions_for_context,
    retry_native_terminal_bind,
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
    *,
    reported_source: str | None = None,
) -> bool:
    """Return whether this startup SessionStart may defer row creation.

    ``session_source`` is the resolved source; ``reported_source`` is the one the CLI
    sent, when resolution changed it (a compaction with no row resolves to startup).
    """
    reported = session_source if reported_source is None else reported_source
    session_source = session_source or "startup"
    if session_source not in STARTUP_SOURCES:
        return False
    if existing_session is not None:
        # An expired row is re-activated through the register path, as before;
        # deferring it would hand the row to lookup recovery with no activation.
        return False

    raw_terminal_context = event.data.get("terminal_context")
    terminal_context = raw_terminal_context if isinstance(raw_terminal_context, dict) else None
    # Identity resolution has run, so a startup the CLI reported is the thread's own.
    terminal_context = enrich_terminal_context_with_cwd(
        terminal_context,
        hook_cwd(event.data, event.cwd),
        external_id=event.session_id,
        machine_id=event.machine_id,
        startup_thread=is_cli_startup(event, reported),
    )
    if is_gobby_acp_child(terminal_context):
        return False
    return True


def is_cli_startup(event: HookEvent, reported_source: str | None) -> bool:
    """Whether the CLI itself sent this SessionStart as its thread's startup.

    A start Gobby built from activity never counts, nor does a start the CLI reported
    as anything else, such as a compaction.
    """
    if event.metadata.get("_synthetic_session_start"):
        return False
    return (reported_source or "startup") in STARTUP_SOURCES


def record_deferred_startup(handler: Any, event: HookEvent, reported_source: str | None) -> None:
    """Leave the durable mark that a thread's own startup deferred its row (#25647, #25905).

    The mark survives a daemon restart before the first prompt. It lets a Codex thread's
    first registration search for its fresh seat, and it holds the pin naming the only CLI
    whose hook may create the row and receive its credential.
    """
    if not is_cli_startup(event, reported_source):
        return
    if handler._session_manager is None or not event.machine_id or not event.session_id:
        return
    try:
        record_startup_mark(
            handler._session_manager.db,
            machine_id=event.machine_id,
            source=event.source.value,
            external_id=event.session_id,
        )
    except psycopg.Error as exc:
        handler.logger.warning(
            "Could not record the startup mark for thread %s: %s", event.session_id, exc
        )


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
            terminal_manager=getattr(handler, "terminal_manager", None),
            terminal_runtime_registry=getattr(handler, "_terminal_runtime_registry", None),
        )
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
            },
        )
    except (json.JSONDecodeError, KeyError, psycopg.Error) as exc:
        handler.logger.warning("Failed to reset agent context injection flag: %s", exc)


def prepare_fresh_session_event(event: HookEvent) -> None:
    """Discard launch authority when a provider creates an independent session."""
    for key in (
        "parent_session_id",
        "workflow_name",
        "agent_depth",
        "agent_name_override",
        "skip_default_agent_activation",
    ):
        event.data.pop(key, None)
    if event.event_type == HookEventType.SESSION_START:
        event.task_id = None
        event.metadata.pop("_task_title", None)
    event.metadata.pop("_parent_session_id", None)


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
) -> list[ContextPart]:
    """Activate agents, seeds, code index, and transcript processing.

    This is the behavior-preserving SessionStart activation body. The return
    value is the additional context passed to session response composition.
    """
    input_data = event.data
    cli_source = event.source.value
    external_id = str(event.session_id or "").strip()
    session_source = str(input_data.get("source", "startup"))

    if terminal_context is None:
        raw_terminal_context = input_data.get("terminal_context")
        terminal_context = raw_terminal_context if isinstance(raw_terminal_context, dict) else None
        terminal_context = enrich_terminal_context_with_cwd(
            terminal_context,
            hook_cwd(input_data, event.cwd),
            external_id=external_id,
            machine_id=event.machine_id,
        )

    if session_obj is None and handler._session_manager:
        session_obj = handler._session_manager.get(session_id)
    fresh_boundary = session_source in {"clear", "new"} or (
        session_source in {"startup", "", "resume"} and getattr(resolution, "session", None) is None
    )
    if fresh_boundary and getattr(session_obj, "agent_run_id", None) is None:
        prepare_fresh_session_event(event)
        if handler._session_manager is not None:
            from gobby.hooks.event_handlers.clear_identity import CLEAR_IDENTITY_VARIABLE
            from gobby.workflows.state_manager import SessionVariableManager

            SessionVariableManager(handler._session_manager.db).merge_variables(
                session_id, {"_agent_type": "default", CLEAR_IDENTITY_VARIABLE: True}
            )
    workflow_name = input_data.get("workflow_name")
    parent_session_id = input_data.get("parent_session_id")
    project_id = project_id or event.project_id or getattr(session_obj, "project_id", None)
    if transcript_path is None:
        transcript_path = input_data.get("transcript_path") or getattr(
            session_obj,
            "transcript_path",
            None,
        )

    pending_native_terminal_bind = discover_and_bind_external_terminal(
        handler,
        session_id=session_id,
        project_id=project_id,
        terminal_context=terminal_context,
    )

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

    if session_obj is not None and fresh_boundary and handler._session_manager is not None:
        try:
            handler._session_manager.bind_native_clear_successor(session_id)
        except Exception as exc:
            handler.logger.warning(
                "Failed to retire previous terminal session %s: %s", session_id, exc
            )
    if handler._session_manager is not None:
        try:
            from gobby.agents.agent_workspaces import bind_epic_workspace
            from gobby.agents.workspace_bindings import bind_session_workspace

            bind_session_workspace(handler._session_manager.db, session_id)
            bind_epic_workspace(handler._session_manager.db, session_id)
        except Exception as exc:
            handler.logger.warning(
                "Failed to bind agent workspace for session %s: %s", session_id, exc
            )
    rehydrate_found_work_gate_arm(handler, session_id)
    # Retire the old identity before retrying physical terminal ownership.
    expire_stale_terminal_sessions_for_context(
        handler,
        session_id=session_id,
        project_id=project_id,
        terminal_context=terminal_context,
    )
    # A reused native terminal can still be held by the live predecessor until
    # context expiry; retry only the bind that the ownership guard refused.
    retry_native_terminal_bind(
        handler,
        session_id=session_id,
        pending_bind=pending_native_terminal_bind,
    )
    # /clear left the composer, and no BEFORE_AGENT records it; the pane is bound by now.
    if session_source == "clear":
        handler._record_composer_submit(session_id)

    context_decision = classify_session_start_context(
        handler,
        session_id=session_id,
        session=session_obj,
        session_source=session_source,
        is_existing_session=False,
        owner_token=_startup_claim_owner_token(event),
    )
    event.metadata[_CONTEXT_MODE_METADATA_KEY] = context_decision.mode
    stash_startup_claim(event.metadata, context_decision)

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

    additional_context: list[ContextPart] = []
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
            additional_context.append(("claimed_tasks", claimed_ctx))

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
        additional_context.append(
            (
                "active_task",
                f"## Active Task Context\n\nYou are working on task: {task_title} ({event.task_id})",
            )
        )

    if session_obj:
        _consume_pending_handoff_compact_continuation(
            handler,
            session_source=session_source,
            pending_session_id=session_id,
            target_session=session_obj,
        )

    return additional_context
