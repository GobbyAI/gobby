"""Session-start orchestration."""

from __future__ import annotations

import json
import time
from typing import Any, cast

import psycopg

from gobby.hooks.events import HookEvent, HookResponse
from gobby.hooks.project_context import resolve_hook_project_context
from gobby.hooks.terminal_context import enrich_terminal_context_with_cwd, hook_cwd

from .agents import _seed_memory_recall_vars
from .context import classify_session_start_context, mark_startup_context_injected
from .handoff import find_parent_session, populate_handoff_session_variables
from .types import AgentActivationResult

SLOW_SESSION_START_THRESHOLD_MS = 1000


def _compat_module() -> Any:
    import gobby.hooks.event_handlers._session_start as session_start

    return session_start


def _log_session_start_lifecycle(
    handler: Any,
    *,
    session_id: str | None,
    session_source: str | None,
    cli_source: str,
    project_id: str | None,
    parent_session_id: str | None,
    pre_created: bool = False,
) -> None:
    parts = ["Session start:"]
    if session_id:
        parts.append(f"session={session_id}")
    if session_source:
        parts.append(f"source={session_source}")
    parts.append(f"cli={cli_source}")
    if project_id:
        parts.append(f"project={project_id}")
    if parent_session_id:
        parts.append(f"parent={parent_session_id}")
    if pre_created:
        parts.append("pre_created=true")
    handler.logger.info(" ".join(parts))


def _log_session_start_timing(
    handler: Any,
    *,
    session_source: str,
    session_id: str | None,
    timings: dict[str, int],
) -> None:
    timing_message = f"SESSION_START timing [{session_source}]: " + " ".join(
        f"{name}={duration}ms" for name, duration in timings.items()
    )
    slow_component, slow_ms = max(
        ((name, duration) for name, duration in timings.items() if name != "total"),
        key=lambda item: item[1],
        default=("total", timings.get("total", 0)),
    )
    total_ms = timings.get("total", 0)
    if total_ms >= SLOW_SESSION_START_THRESHOLD_MS:
        handler.logger.info(
            "SESSION_START slow: "
            f"component={slow_component} duration={slow_ms}ms "
            f"total={total_ms}ms source={session_source} session={session_id}",
        )
    else:
        handler.logger.debug(timing_message)


def _consume_pending_compact_self_continuation(
    handler: Any,
    *,
    pending_session_id: str | None,
    target_session: Any,
    fallback_pending_session_id: str | None = None,
) -> bool:
    """Consume compact_self markers for providers that do not tag compact restarts."""
    if not handler._session_manager:
        return False
    return bool(
        _compat_module().consume_and_schedule_compact_self_continuation(
            handler._session_manager.db,
            pending_session_id=pending_session_id,
            fallback_pending_session_id=fallback_pending_session_id,
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
    handler.logger.info(
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
            {"_agent_context_injected": False},
        )
    except (json.JSONDecodeError, TypeError, psycopg.Error) as e:
        handler.logger.warning(f"Failed to reset agent context injection flag: {e}")


def handle_session_start(handler: Any, event: HookEvent) -> HookResponse:
    """Handle SESSION_START event."""
    _t0 = time.monotonic()
    external_id = event.session_id
    input_data = event.data
    transcript_path = input_data.get("transcript_path")
    cli_source = event.source.value
    cwd = hook_cwd(input_data, event.cwd)

    if not transcript_path:
        transcript_path = handler._derive_transcript_path(cli_source, input_data, external_id)
    session_source = input_data.get("source", "startup")
    _t_pre_check = time.monotonic()

    existing_session = None
    raw_terminal_context = input_data.get("terminal_context")
    terminal_context = raw_terminal_context if isinstance(raw_terminal_context, dict) else None
    terminal_context = enrich_terminal_context_with_cwd(terminal_context, cwd)

    if terminal_context and terminal_context.get("gobby_acp_child") == "1":
        handler.logger.info(
            "Skipping session registration for ACP child process",
            extra={
                "cli": cli_source,
                "external_id": external_id,
                "gobby_acp_child": terminal_context.get("gobby_acp_child"),
            },
        )
        return HookResponse()
    gobby_session_id_from_env = (
        terminal_context.get("gobby_session_id") if terminal_context else None
    )

    if handler._session_manager:
        try:
            existing_session = handler._session_manager.get(external_id)
            if existing_session:
                return cast(
                    HookResponse,
                    handler._handle_pre_created_session(
                        existing_session=existing_session,
                        external_id=external_id,
                        transcript_path=transcript_path,
                        cli_source=cli_source,
                        event=event,
                        cwd=cwd,
                        terminal_context=terminal_context,
                    ),
                )
        except Exception as e:
            handler.logger.debug(f"No pre-created session found by external_id: {e}")

        if gobby_session_id_from_env and not existing_session:
            try:
                existing_session = handler._session_manager.get(gobby_session_id_from_env)
                if existing_session:
                    handler.logger.info(
                        f"Found pre-created session {gobby_session_id_from_env} via "
                        f"terminal_context, updating external_id to {external_id}"
                    )
                    handler._session_manager.update(
                        gobby_session_id_from_env,
                        external_id=external_id,
                        terminal_context=terminal_context,
                    )
                    if handler._session_manager:
                        handler._session_manager.cache_session_mapping(
                            external_id=external_id,
                            source=cli_source,
                            session_id=gobby_session_id_from_env,
                        )
                    return cast(
                        HookResponse,
                        handler._handle_pre_created_session(
                            existing_session=existing_session,
                            external_id=external_id,
                            transcript_path=transcript_path,
                            cli_source=cli_source,
                            event=event,
                            cwd=cwd,
                            terminal_context=terminal_context,
                        ),
                    )
            except Exception as e:
                handler.logger.debug(f"No pre-created session found by gobby_session_id: {e}")

    project_context_session_manager = None if input_data.get("cwd") else handler._session_manager
    project_resolution = resolve_hook_project_context(
        event,
        session_manager=project_context_session_manager,
        resolve_project_id=handler._resolve_project_id,
        logger=handler.logger,
    )
    if project_resolution.skipped:
        handler.logger.debug(
            "Skipping SESSION_START without project context: %s",
            project_resolution.reason,
        )
        return HookResponse(decision="allow")
    project_id = project_resolution.project_id
    if project_id is None:
        return HookResponse(decision="allow")

    machine_id = handler._get_machine_id()

    handler.logger.debug(
        f"SESSION_START: cli={cli_source}, project={project_id}, source={session_source}"
    )

    if handler._session_manager:
        try:
            existing_web_chat = handler._session_manager.find_by_external_id(
                external_id,
                machine_id,
                project_id,
                cli_source,
                session_type="web_chat",
            )
            if (
                existing_web_chat is not None
                and isinstance(getattr(existing_web_chat, "id", None), str)
                and getattr(existing_web_chat, "session_type", None) == "web_chat"
            ):
                handler.logger.info(
                    "Found web-chat session %s by external_id %s; reusing it",
                    existing_web_chat.id,
                    external_id,
                )
                return cast(
                    HookResponse,
                    handler._handle_pre_created_session(
                        existing_session=existing_web_chat,
                        external_id=external_id,
                        transcript_path=transcript_path,
                        cli_source=cli_source,
                        event=event,
                        cwd=cwd,
                        terminal_context=terminal_context,
                    ),
                )
        except Exception as e:
            handler.logger.debug(f"No web-chat session found by external_id: {e}")

    _t_parent = time.monotonic()
    workflow_name = input_data.get("workflow_name")
    agent_depth = input_data.get("agent_depth")
    parent_session_id, session_source = find_parent_session(
        handler,
        input_data,
        session_source,
        machine_id,
        project_id,
        cli_source,
    )

    _t_register = time.monotonic()
    agent_depth_val = 0
    if agent_depth:
        try:
            agent_depth_val = int(agent_depth)
        except (ValueError, TypeError):
            pass
    sandbox_enabled = input_data.get("sandbox_enabled")
    sandbox_enabled_val = sandbox_enabled if isinstance(sandbox_enabled, bool) else None

    session_id = None
    if handler._session_manager:
        session_id = handler._session_manager.register_session(
            external_id=external_id,
            machine_id=machine_id,
            project_id=project_id,
            parent_session_id=parent_session_id,
            transcript_path=transcript_path,
            source=cli_source,
            project_path=cwd,
            terminal_context=terminal_context,
            workflow_name=workflow_name,
            agent_depth=agent_depth_val,
            sandbox_enabled=sandbox_enabled_val,
        )

    if parent_session_id and handler._session_manager:
        try:
            handler._session_manager.mark_session_expired(parent_session_id)
            handler.logger.debug(f"Marked parent session {parent_session_id} as expired")
        except Exception as e:
            handler.logger.warning(f"Failed to mark parent session as expired: {e}")

    handler._setup_code_index(session_id, project_id)

    if workflow_name and session_id:
        handler.logger.debug(
            "Pipeline workflow registered for session -- agent will execute via run_pipeline",
            extra={"workflow_name": workflow_name, "session_id": session_id},
        )

    if session_id and handler._session_manager is not None:
        try:
            _seed_memory_recall_vars(handler, session_id)
        except Exception as e:
            handler.logger.warning(f"Failed to seed memory recall vars: {e}")

    session_obj = None
    if session_id and handler._session_manager:
        session_obj = handler._session_manager.get(session_id)
    if session_obj:
        _schedule_tmux_window_rename_for_session(handler, session_obj)

    context_decision = classify_session_start_context(
        handler,
        session_id=session_id,
        session=session_obj,
        session_source=session_source,
        is_existing_session=False,
    )

    _t_activate = time.monotonic()
    agent_result: AgentActivationResult | None = None
    if session_id and not input_data.get("skip_default_agent_activation"):
        try:
            agent_override = input_data.get("agent_name_override")
            agent_result = handler._activate_default_agent(
                session_id,
                cli_source,
                project_id,
                agent_name_override=agent_override,
            )
        except Exception as e:
            handler.logger.error(f"Failed to activate default agent: {e}", exc_info=True)

    _t_track = time.monotonic()
    if transcript_path and handler._session_coordinator:
        try:
            handler._session_coordinator.register_session(external_id)
        except Exception as e:
            handler.logger.error(f"Failed to setup session tracking: {e}", exc_info=True)

    if session_id:
        event.metadata["_platform_session_id"] = session_id
    if parent_session_id:
        event.metadata["_parent_session_id"] = parent_session_id

    _t_msg_proc = time.monotonic()
    if handler._message_processor and transcript_path and session_id:
        try:
            handler._message_processor.register_session(
                session_id,
                transcript_path,
                source=cli_source,
            )
        except Exception as e:
            handler.logger.warning(f"Failed to register session with message processor: {e}")

    _t_handoff = time.monotonic()
    additional_context: list[str] = []
    if context_decision.mode == "full":
        _reset_agent_context_injection(handler, session_id)

    populate_handoff_session_variables(handler, session_id, parent_session_id, session_source)

    if session_id and project_id and not event.task_id:
        claimed_ctx = handler._build_claimed_task_context(
            session_id,
            project_id,
            compact=context_decision.mode == "live",
        )
        if claimed_ctx:
            additional_context.append(claimed_ctx)

    if event.task_id and session_id and handler._session_manager:
        task_title = event.metadata.get("_task_title", "Unknown Task")
        task_context_str = f"You are working on task: {task_title} ({event.task_id})"
        try:
            from gobby.workflows.state_manager import SessionVariableManager

            SessionVariableManager(handler._session_manager.db).merge_variables(
                session_id,
                {"task_context": task_context_str},
            )
        except (json.JSONDecodeError, TypeError, psycopg.Error) as e:
            handler.logger.warning(f"Failed to persist task context: {e}")

    if event.task_id:
        task_title = event.metadata.get("_task_title", "Unknown Task")
        additional_context.append("\n## Active Task Context\n")
        additional_context.append(f"You are working on task: {task_title} ({event.task_id})")

    if session_obj:
        _consume_pending_compact_self_continuation(
            handler,
            pending_session_id=session_id,
            fallback_pending_session_id=parent_session_id if session_source == "compact" else None,
            target_session=session_obj,
        )

    claimed_tasks_info = handler._get_claimed_task_info(session_id, project_id)

    def _ms(a: float, b: float) -> int:
        return int((b - a) * 1000)

    _t_end = time.monotonic()
    _log_session_start_lifecycle(
        handler,
        session_id=session_id,
        session_source=session_source,
        cli_source=cli_source,
        project_id=project_id,
        parent_session_id=parent_session_id,
    )
    _log_session_start_timing(
        handler,
        session_source=session_source,
        session_id=session_id,
        timings={
            "pre_check": _ms(_t0, _t_pre_check),
            "session_lookup": _ms(_t_pre_check, _t_parent),
            "parent": _ms(_t_parent, _t_register),
            "register": _ms(_t_register, _t_activate),
            "activate_agent": _ms(_t_activate, _t_track),
            "track": _ms(_t_track, _t_msg_proc),
            "msg_proc": _ms(_t_msg_proc, _t_handoff),
            "handoff": _ms(_t_handoff, _t_end),
            "total": _ms(_t0, _t_end),
        },
    )

    response = cast(
        HookResponse,
        handler._compose_session_response(
            session=session_obj,
            session_id=session_id,
            external_id=external_id,
            parent_session_id=parent_session_id,
            machine_id=machine_id,
            project_id=project_id,
            task_id=event.task_id,
            additional_context=additional_context,
            terminal_context=terminal_context,
            agent_info=agent_result,
            session_source=session_source,
            claimed_tasks_info=claimed_tasks_info,
        ),
    )
    if context_decision.mode == "full":
        mark_startup_context_injected(handler, session_id)
    return response


def handle_pre_created_session(
    handler: Any,
    existing_session: Any,
    external_id: str,
    transcript_path: str | None,
    cli_source: str,
    event: HookEvent,
    cwd: str | None,
    terminal_context: dict[str, Any] | None = None,
) -> HookResponse:
    """Handle session start for a pre-created session."""
    handler.logger.info(f"Found pre-created session {external_id}, updating instead of creating")

    if not transcript_path:
        input_data = event.data if event else {}
        transcript_path = handler._derive_transcript_path(cli_source, input_data, external_id)

    session_obj = existing_session
    if handler._session_manager:
        updated = handler._session_manager.update(
            session_id=existing_session.id,
            transcript_path=transcript_path,
            status="active",
        )
        if updated is not None:
            session_obj = updated

    tmux_pane_added = False
    if handler._session_manager and terminal_context:
        refreshed, tmux_pane_added = handler._session_manager.backfill_terminal_context(
            existing_session.id,
            terminal_context,
        )
        if refreshed is not None:
            session_obj = refreshed

    if tmux_pane_added or (
        isinstance(getattr(session_obj, "terminal_context", None), dict)
        and session_obj.terminal_context.get("tmux_pane")
    ):
        _schedule_tmux_window_rename_for_session(handler, session_obj)

    if handler._session_manager:
        handler._session_manager.cache_session_mapping(
            external_id=external_id,
            source=cli_source,
            session_id=existing_session.id,
        )

    session_id = session_obj.id
    parent_session_id = session_obj.parent_session_id
    machine_id = handler._get_machine_id()

    if transcript_path and handler._session_coordinator:
        try:
            handler._session_coordinator.register_session(external_id)
        except Exception as e:
            handler.logger.error(f"Failed to setup session tracking: {e}")

    if session_obj.agent_run_id and handler._session_coordinator:
        try:
            handler._session_coordinator.start_agent_run(session_obj.agent_run_id)
        except Exception as e:
            handler.logger.warning(f"Failed to start agent run: {e}")
    if session_obj.agent_run_id:
        _record_agent_run_native_session(handler, session_obj.agent_run_id, external_id)

    if session_obj.workflow_name and session_id:
        handler.logger.debug(
            "Pipeline workflow registered for session -- agent will execute via run_pipeline",
            extra={"workflow_name": session_obj.workflow_name, "session_id": session_id},
        )

    handler._setup_code_index(session_id, session_obj.project_id)

    if handler._session_manager is not None:
        try:
            _seed_memory_recall_vars(handler, session_id)
        except Exception as e:
            handler.logger.warning(f"Failed to seed memory recall vars: {e}")

    agent_result: AgentActivationResult | None = None
    input_data = event.data if event else {}
    session_source = input_data.get("source", "startup")
    context_decision = classify_session_start_context(
        handler,
        session_id=session_id,
        session=session_obj,
        session_source=session_source,
        is_existing_session=True,
    )

    try:
        agent_override = input_data.get("agent_name_override")
        agent_result = handler._activate_default_agent(
            session_id,
            cli_source,
            session_obj.project_id,
            agent_name_override=agent_override,
        )
    except Exception as e:
        handler.logger.error(
            f"Failed to activate default agent for pre-created session: {e}",
            exc_info=True,
        )

    event.metadata["_platform_session_id"] = session_id

    if handler._message_processor and transcript_path:
        try:
            handler._message_processor.register_session(
                session_id,
                transcript_path,
                source=cli_source,
            )
        except Exception as e:
            handler.logger.warning(f"Failed to register with message processor: {e}")

    additional_context: list[str] = []
    if context_decision.mode == "full":
        _reset_agent_context_injection(handler, session_id)

    if session_id and session_obj.project_id and not event.task_id:
        claimed_ctx = handler._build_claimed_task_context(
            session_id,
            session_obj.project_id,
            compact=context_decision.mode == "live",
        )
        if claimed_ctx:
            additional_context.append(claimed_ctx)

    claimed_tasks_info = handler._get_claimed_task_info(session_id, session_obj.project_id)

    _consume_pending_compact_self_continuation(
        handler,
        pending_session_id=session_id,
        target_session=session_obj,
    )

    _log_session_start_lifecycle(
        handler,
        session_id=session_id,
        session_source=session_source,
        cli_source=cli_source,
        project_id=session_obj.project_id,
        parent_session_id=parent_session_id,
        pre_created=True,
    )

    response = cast(
        HookResponse,
        handler._compose_session_response(
            session=session_obj,
            session_id=session_id,
            external_id=external_id,
            parent_session_id=parent_session_id,
            machine_id=machine_id,
            project_id=session_obj.project_id,
            task_id=event.task_id,
            additional_context=additional_context,
            is_pre_created=True,
            terminal_context=session_obj.terminal_context,
            agent_info=agent_result,
            session_source=session_source,
            claimed_tasks_info=claimed_tasks_info,
        ),
    )
    if context_decision.mode == "full":
        mark_startup_context_injected(handler, session_id)
    return response


def _record_agent_run_native_session(handler: Any, run_id: str, external_id: str) -> None:
    """Store provider-native session id in the run's resume metadata when discovered."""
    if not external_id or handler._session_manager is None:
        return
    try:
        from gobby.storage.agents import LocalAgentRunManager

        manager = LocalAgentRunManager(handler._session_manager.db)
        run = manager.get(run_id)
        if run is None:
            return
        metadata = dict(run.resume_metadata_json or {})
        if metadata.get("provider_native_session_id") == external_id:
            return
        metadata["provider_native_session_id"] = external_id
        manager.update_resume_metadata(run_id, metadata)
    except psycopg.Error as exc:
        handler.logger.debug(
            "Failed to persist provider-native session id for agent run %s: %s",
            run_id,
            exc,
        )
