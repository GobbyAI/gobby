"""Session-start orchestration."""

from __future__ import annotations

import json
import time
from typing import Any, cast

import psycopg

from gobby.hooks.envelope_dedupe import bump_stop_replay_epoch
from gobby.hooks.events import HookEvent, HookResponse
from gobby.hooks.grok_pending_context import clear_queued_context
from gobby.hooks.project_context import resolve_hook_project_context
from gobby.hooks.terminal_context import (
    enrich_terminal_context_with_cwd,
    hook_cwd,
    is_gobby_acp_child,
)
from gobby.storage.session_activity import reconcile_compact_session_activity
from gobby.storage.sessions._update_sentinel import UNSET

from .agents import _seed_parent_turn_seq, _seed_wiki_overview_var
from .context import (
    classify_session_start_context,
    mark_startup_context_injected,
)
from .context import (
    startup_claim_owner_token as _startup_claim_owner_token,
)
from .handoff import (
    STARTUP_SOURCES,
    rebind_resumed_session_start,
    resolve_session_start_identity,
)
from .materialize import (
    _CONTEXT_MODE_METADATA_KEY,
    _consume_pending_handoff_compact_continuation,
    _reset_agent_context_injection,
    _schedule_tmux_window_rename_for_session,
    session_start_should_defer,
)
from .profile import seed_user_profile_content
from .terminal_runtime import (
    session_start_is_native_subagent_child,
    session_start_is_nested_cli_child,
)
from .transcripts import replace_session_message_processor

SLOW_SESSION_START_THRESHOLD_MS = 1000


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
    handler.logger.debug(" ".join(parts))


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
            "SESSION_START slow: component=%s duration=%sms total=%sms source=%s session=%s",
            slow_component,
            slow_ms,
            total_ms,
            session_source,
            session_id,
        )
    else:
        handler.logger.debug(timing_message)


def handle_session_start(handler: Any, event: HookEvent) -> HookResponse:
    """Handle SESSION_START event."""
    _t0 = time.monotonic()
    input_data = event.data
    cli_source = event.source.value
    external_id = str(event.session_id or "").strip()
    if not external_id:
        handler.logger.warning(
            "Skipping SESSION_START without external session id",
            extra={"cli": cli_source},
        )
        return HookResponse(decision="allow")

    transcript_path: str | None = None
    cwd = hook_cwd(input_data, event.cwd)

    session_source = input_data.get("source", "startup")
    _t_pre_check = time.monotonic()

    existing_session = None
    raw_terminal_context = input_data.get("terminal_context")
    terminal_context = raw_terminal_context if isinstance(raw_terminal_context, dict) else None
    terminal_context = enrich_terminal_context_with_cwd(terminal_context, cwd)

    if is_gobby_acp_child(terminal_context):
        handler.logger.info(
            "Skipping session registration for ACP child process",
            extra={
                "cli": cli_source,
                "external_id": external_id,
                "gobby_acp_child": "1",
            },
        )
        return HookResponse()
    if session_start_is_nested_cli_child(cli_source, terminal_context):
        handler.logger.info(
            "Skipping session registration for nested CLI child process",
            extra={
                "cli": cli_source,
                "external_id": external_id,
                "tmux_pane": terminal_context.get("tmux_pane") if terminal_context else None,
            },
        )
        return HookResponse(decision="allow")
    if (
        (session_source or "startup") in STARTUP_SOURCES
        and handler._session_manager
        and session_start_is_native_subagent_child(
            handler._session_manager,
            terminal_context,
            event.machine_id or handler._get_machine_id(),
        )
    ):
        handler.logger.info(
            "Skipping session registration for native subagent inheriting TTY",
            extra={
                "cli": cli_source,
                "external_id": external_id,
                "tmux_pane": terminal_context.get("tmux_pane") if terminal_context else None,
            },
        )
        return HookResponse(decision="allow")
    gobby_session_id_from_env = (
        terminal_context.get("gobby_session_id") if terminal_context else None
    )

    if handler._session_manager and session_source != "clear":
        try:
            existing_session = handler._session_manager.get(external_id)
            if existing_session:
                inactive = getattr(existing_session, "status", None) in {"expired", "deleted"}
                if inactive and session_source not in {"resume", "compact"}:
                    return HookResponse(decision="allow")
                if not inactive:
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
            handler.logger.debug("No pre-created session found by external_id: %s", e)

        if gobby_session_id_from_env and not existing_session:
            try:
                existing_session = handler._session_manager.get(gobby_session_id_from_env)
                if existing_session:
                    inactive = getattr(existing_session, "status", None) in {"expired", "deleted"}
                    if inactive and session_source not in {"resume", "compact"}:
                        return HookResponse(decision="allow")
                    if not inactive:
                        handler.logger.info(
                            "Found pre-created session %s via terminal_context, updating external_id to %s",
                            gobby_session_id_from_env,
                            external_id,
                        )
                        handler._session_manager.update(
                            gobby_session_id_from_env,
                            external_id=external_id,
                        )
                        handler._session_manager.cache_session_mapping(
                            external_id=external_id,
                            source=cli_source,
                            session_id=gobby_session_id_from_env,
                            project_id=existing_session.project_id,
                            session_type=existing_session.session_type,
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
                handler.logger.debug("No pre-created session found by gobby_session_id: %s", e)

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

    machine_id = event.machine_id or handler._get_machine_id()

    handler.logger.debug(
        "SESSION_START: cli=%s, project=%s, source=%s", cli_source, project_id, session_source
    )

    if handler._session_manager and session_source != "clear":
        try:
            existing_web_chat = handler._session_manager.find_by_external_id(
                external_id,
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
            handler.logger.debug("No web-chat session found by external_id: %s", e)

    _t_parent = time.monotonic()
    workflow_name = input_data.get("workflow_name")
    agent_depth = input_data.get("agent_depth")
    resolution = resolve_session_start_identity(
        handler,
        input_data,
        session_source,
        external_id=external_id,
        machine_id=machine_id,
        project_id=project_id,
        cli_source=cli_source,
        terminal_context=terminal_context,
    )
    if resolution.blocked_reason:
        return HookResponse(decision="block", reason=resolution.blocked_reason)
    session_source = resolution.session_source
    if session_start_should_defer(event, resolution.session, session_source):
        return HookResponse(decision="allow")
    parent_session_id = input_data.get("parent_session_id")

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
    if handler._session_manager and session_source == "compact" and resolution.session is not None:
        canonical_session = resolution.session
        activity = reconcile_compact_session_activity(
            handler._session_manager,
            canonical_session.id,
        )
        if not activity.success:
            detail = activity.error_result()
            handler.logger.warning(
                "Blocking compact session reactivation: %s",
                detail["error"],
                extra={
                    "event": "compact_identity_reactivation_blocked",
                    "session_id": canonical_session.id,
                    **detail,
                },
            )
            return HookResponse(
                decision="block",
                reason=f"{detail['error_code']}: {detail['error']}",
            )

        observed_external_id = external_id
        external_id = canonical_session.external_id
        event.session_id = external_id
        if observed_external_id != external_id:
            event.metadata["_observed_external_id"] = observed_external_id
        transcript_path = handler._derive_transcript_path(
            cli_source,
            input_data,
            external_id,
            owner_machine_id=canonical_session.machine_id,
            local_machine_id=machine_id,
            stored_path=canonical_session.transcript_path,
        )
        input_data["transcript_path"] = transcript_path
        session_id = canonical_session.id
        handler._session_manager.cache_session_mapping(
            external_id=external_id,
            source=cli_source,
            session_id=session_id,
            project_id=canonical_session.project_id,
            session_type=canonical_session.session_type,
        )
    elif handler._session_manager and session_source == "resume" and resolution.session is not None:
        try:
            resumed, transcript_path = rebind_resumed_session_start(
                handler,
                input_data,
                resolution.session,
                machine_id=machine_id,
                project_id=project_id,
                cli_source=cli_source,
                terminal_context=terminal_context,
                transcript_path=transcript_path,
            )
        except Exception as exc:
            handler.logger.warning("Explicit session resume failed: %s", exc)
            return HookResponse(decision="block", reason=str(exc))
        session_id = resumed.id
        external_id = resumed.external_id
        event.session_id = external_id
    elif handler._session_manager:
        transcript_path = handler._derive_transcript_path(
            cli_source,
            input_data,
            external_id,
            owner_machine_id=machine_id,
            local_machine_id=machine_id,
        )
        session_id = handler._session_manager.register_session(
            external_id=external_id,
            machine_id=machine_id,
            project_id=project_id,
            parent_session_id=parent_session_id if parent_session_id else UNSET,
            transcript_path=transcript_path,
            source=cli_source,
            project_path=cwd,
            terminal_context=terminal_context,
            workflow_name=workflow_name,
            agent_depth=agent_depth_val,
            sandbox_enabled=sandbox_enabled_val,
        )

    if handler._session_manager and session_source == "compact":
        if not session_id:
            return HookResponse(
                decision="block",
                reason="Compact reactivation did not return a session ID.",
            )
    elif parent_session_id and session_id and handler._session_manager:
        if parent_session_id != session_id:
            try:
                handler._session_manager.mark_session_expired(
                    parent_session_id,
                    cause="parent_registration",
                )
                handler.logger.debug("Marked parent session %s as expired", parent_session_id)
            except Exception as e:
                handler.logger.warning("Failed to mark parent session as expired: %s", e)

    _t_activate = time.monotonic()
    if session_id:
        event.project_id = project_id
        session_obj = handler._session_manager.get(session_id) if handler._session_manager else None
        additional_context = handler._activate_materialized_session(
            event,
            session_id,
            resolution=resolution,
            session_obj=session_obj,
            project_id=project_id,
            transcript_path=transcript_path,
            terminal_context=terminal_context,
        )
        context_mode = str(event.metadata.pop(_CONTEXT_MODE_METADATA_KEY, "live"))
        if session_source == "clear" and handler._session_manager:
            rebound = handler._session_manager.get(session_id)
            if rebound is not None:
                session_obj = rebound
    else:
        handler._setup_code_index(None, project_id)
        session_obj = None
        context_decision = classify_session_start_context(
            handler,
            session_id=None,
            session=None,
            session_source=session_source,
            is_existing_session=False,
            owner_token=_startup_claim_owner_token(event),
        )
        context_mode = context_decision.mode
        additional_context = []
        if event.task_id:
            task_title = event.metadata.get("_task_title", "Unknown Task")
            additional_context.extend(
                [
                    "\n## Active Task Context\n",
                    f"You are working on task: {task_title} ({event.task_id})",
                ]
            )

    effective_parent_session_id = parent_session_id or getattr(
        session_obj,
        "parent_session_id",
        None,
    )
    _t_track = time.monotonic()
    _t_msg_proc = _t_track
    _t_handoff = _t_track

    def _ms(a: float, b: float) -> int:
        return int((b - a) * 1000)

    _t_end = time.monotonic()
    _log_session_start_lifecycle(
        handler,
        session_id=session_id,
        session_source=session_source,
        cli_source=cli_source,
        project_id=project_id,
        parent_session_id=effective_parent_session_id,
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

    clear_queued_context(handler._session_manager, session_id)
    bump_stop_replay_epoch()
    response = cast(
        HookResponse,
        handler._compose_session_response(
            session=session_obj,
            session_id=session_id,
            external_id=external_id,
            parent_session_id=effective_parent_session_id,
            machine_id=machine_id,
            project_id=project_id,
            task_id=event.task_id,
            additional_context=additional_context,
            terminal_context=terminal_context,
        ),
    )
    if context_mode == "full":
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
    handler.logger.info("Found pre-created session %s, updating instead of creating", external_id)

    input_data = event.data if event else {}
    if transcript_path and "transcript_path" not in input_data:
        input_data = {**input_data, "transcript_path": transcript_path}
    local_machine_id = event.machine_id or handler._get_machine_id()
    transcript_path = handler._derive_transcript_path(
        cli_source,
        input_data,
        external_id,
        owner_machine_id=existing_session.machine_id,
        local_machine_id=local_machine_id,
        stored_path=getattr(existing_session, "transcript_path", None),
    )

    session_obj = existing_session
    if handler._session_manager:
        updated = handler._session_manager.update(
            session_id=existing_session.id,
            transcript_path=transcript_path if transcript_path else UNSET,
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
            project_id=session_obj.project_id,
            session_type=session_obj.session_type,
        )

    session_id = session_obj.id
    parent_session_id = session_obj.parent_session_id
    machine_id = event.machine_id or session_obj.machine_id or handler._get_machine_id()

    if transcript_path and handler._session_coordinator:
        try:
            handler._session_coordinator.register_session(external_id)
        except Exception as e:
            handler.logger.error("Failed to setup session tracking: %s", e)

    if session_obj.agent_run_id and handler._session_coordinator:
        try:
            handler._session_coordinator.start_agent_run(session_obj.agent_run_id)
        except Exception as e:
            handler.logger.warning("Failed to start agent run: %s", e)
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
            _seed_parent_turn_seq(handler, session_id)
        except Exception as e:
            handler.logger.warning("Failed to seed memory recall vars: %s", e)
        _seed_wiki_overview_var(handler, session_id, session_obj.project_id)

    input_data = event.data if event else {}
    session_source = input_data.get("source", "startup")
    context_decision = classify_session_start_context(
        handler,
        session_id=session_id,
        session=session_obj,
        session_source=session_source,
        is_existing_session=True,
        owner_token=_startup_claim_owner_token(event),
    )

    if not input_data.get("skip_default_agent_activation"):
        try:
            agent_override = input_data.get("agent_name_override")
            handler._activate_default_agent(
                session_id,
                cli_source,
                session_obj.project_id,
                agent_name_override=agent_override,
            )
        except Exception as e:
            handler.logger.exception(
                "Failed to activate default agent for pre-created session: %s",
                e,
            )

    if handler._session_manager is not None:
        try:
            seed_user_profile_content(handler, session_id)
        except (KeyError, json.JSONDecodeError, psycopg.Error) as e:
            handler.logger.warning("Failed to seed user profile vars: %s", e)

    event.metadata["_platform_session_id"] = session_id

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
        except Exception as e:
            handler.logger.warning("Failed to register with message processor: %s", e)

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

    _consume_pending_handoff_compact_continuation(
        handler,
        session_source=session_source,
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

    clear_queued_context(handler._session_manager, session_id)
    bump_stop_replay_epoch()
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
        if (
            run is None
            or (run.resume_metadata_json or {}).get("provider_native_session_id") == external_id
        ):
            return
        manager.merge_resume_metadata(
            run_id,
            {"provider_native_session_id": external_id},
        )
    except psycopg.Error as exc:
        handler.logger.debug(
            "Failed to persist provider-native session id for agent run %s: %s",
            run_id,
            exc,
        )
