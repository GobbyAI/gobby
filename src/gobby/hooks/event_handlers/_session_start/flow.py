"""Session-start orchestration."""

from __future__ import annotations

import time
from typing import Any, cast

from gobby.hooks.events import HookEvent, HookResponse
from gobby.hooks.project_context import resolve_hook_project_context

from .handoff import find_parent_session, populate_handoff_session_variables
from .types import AgentActivationResult


def _compat_module() -> Any:
    import gobby.hooks.event_handlers._session_start as session_start

    return session_start


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


def handle_session_start(handler: Any, event: HookEvent) -> HookResponse:
    """Handle SESSION_START event."""
    _t0 = time.monotonic()
    external_id = event.session_id
    input_data = event.data
    transcript_path = input_data.get("transcript_path")
    cli_source = event.source.value
    cwd = input_data.get("cwd")

    if not transcript_path:
        transcript_path = handler._derive_transcript_path(cli_source, input_data, external_id)
    session_source = input_data.get("source", "startup")
    _t_pre_check = time.monotonic()

    existing_session = None
    terminal_context = (
        input_data.get("terminal_context")
        if isinstance(input_data.get("terminal_context"), dict)
        else None
    )

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
    if agent_result and agent_result.context:
        additional_context.append(agent_result.context)

    populate_handoff_session_variables(handler, session_id, parent_session_id, session_source)

    if session_id and project_id and not event.task_id:
        claimed_ctx = handler._build_claimed_task_context(session_id, project_id)
        if claimed_ctx:
            additional_context.append(claimed_ctx)

    if event.task_id and session_id and handler._session_manager:
        task_title = event.metadata.get("_task_title", "Unknown Task")
        task_context_str = f"You are working on task: {task_title} ({event.task_id})"
        from gobby.workflows.state_manager import SessionVariableManager

        SessionVariableManager(handler._session_manager.db).merge_variables(
            session_id,
            {"task_context": task_context_str},
        )

    if event.task_id:
        task_title = event.metadata.get("_task_title", "Unknown Task")
        additional_context.append("\n## Active Task Context\n")
        additional_context.append(f"You are working on task: {task_title} ({event.task_id})")

    session_obj = None
    if session_id and handler._session_manager:
        session_obj = handler._session_manager.get(session_id)

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
    handler.logger.info(
        f"SESSION_START timing [{session_source}]: "
        f"pre_check={_ms(_t0, _t_pre_check)}ms "
        f"parent={_ms(_t_pre_check, _t_parent)}ms "
        f"register={_ms(_t_parent, _t_register)}ms "
        f"activate_agent={_ms(_t_register, _t_activate)}ms "
        f"track={_ms(_t_activate, _t_track)}ms "
        f"msg_proc={_ms(_t_track, _t_msg_proc)}ms "
        f"handoff={_ms(_t_msg_proc, _t_handoff)}ms "
        f"total={_ms(_t0, _t_end)}ms",
    )

    return cast(
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

    if tmux_pane_added:
        title = getattr(session_obj, "title", None)
        if title:
            _compat_module().schedule_tmux_window_rename(
                session_obj,
                title,
                loop=getattr(handler._session_coordinator, "_event_loop", None),
            )

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

    if session_obj.workflow_name and session_id:
        handler.logger.debug(
            "Pipeline workflow registered for session -- agent will execute via run_pipeline",
            extra={"workflow_name": session_obj.workflow_name, "session_id": session_id},
        )

    handler._setup_code_index(session_id, session_obj.project_id)

    agent_result: AgentActivationResult | None = None
    input_data = event.data if event else {}
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
    if agent_result and agent_result.context:
        additional_context.append(agent_result.context)

    if session_id and session_obj.project_id and not event.task_id:
        claimed_ctx = handler._build_claimed_task_context(session_id, session_obj.project_id)
        if claimed_ctx:
            additional_context.append(claimed_ctx)

    claimed_tasks_info = handler._get_claimed_task_info(session_id, session_obj.project_id)

    _consume_pending_compact_self_continuation(
        handler,
        pending_session_id=session_id,
        target_session=session_obj,
    )

    return cast(
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
            claimed_tasks_info=claimed_tasks_info,
        ),
    )
