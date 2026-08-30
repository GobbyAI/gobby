"""Materialize deferred terminal sessions on their first activity hook."""

from __future__ import annotations

from typing import Any, cast

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.event_handlers._session_start.context import mark_startup_context_injected
from gobby.hooks.event_handlers._session_start.handoff import (
    resolve_matching_clear_continuation,
)
from gobby.hooks.event_handlers._session_start.materialize import (
    _CONTEXT_MODE_METADATA_KEY,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.hooks.grok_pending_context import clear_queued_context
from gobby.hooks.terminal_context import enrich_terminal_context_with_cwd, hook_cwd


def build_synthetic_session_start(event: HookEvent, session_id: str) -> HookEvent:
    """Copy deferred identity fields into a SessionStart-shaped event."""
    cwd = hook_cwd(event.data, event.cwd)
    raw_terminal_context = event.data.get("terminal_context")
    terminal_context = raw_terminal_context if isinstance(raw_terminal_context, dict) else None
    terminal_context = enrich_terminal_context_with_cwd(terminal_context, cwd)
    transcript_path = event.data.get("transcript_path")

    data: dict[str, Any] = {"source": "startup"}
    if cwd is not None:
        data["cwd"] = cwd
    if transcript_path is not None:
        data["transcript_path"] = transcript_path
    if terminal_context is not None:
        data["terminal_context"] = terminal_context

    return HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id=event.session_id,
        source=event.source,
        timestamp=event.timestamp,
        data=data,
        machine_id=event.machine_id,
        cwd=cwd,
        user_id=event.user_id,
        project_id=event.project_id,
        task_id=event.task_id,
        workflow_id=event.workflow_id,
        metadata={
            "_platform_session_id": session_id,
            "_synthetic_session_start": True,
        },
    )


def activate_deferred_session(
    manager: Any,
    event: HookEvent,
    blocking_deadline: BlockingEffectDeadline,
) -> HookResponse | None:
    """Activate a just-created row and stage its startup packet on ``event``."""
    session_id = event.metadata.get("_platform_session_id")
    if not isinstance(session_id, str) or not session_id:
        return None

    handlers = manager._event_handlers
    session = manager._session_manager.get(session_id)
    project_id = event.project_id or getattr(session, "project_id", None)
    machine_id = event.machine_id or manager.get_machine_id()
    transcript_path = handlers._derive_transcript_path(
        event.source.value,
        event.data,
        str(event.session_id or ""),
        owner_machine_id=machine_id,
        local_machine_id=machine_id,
        stored_path=getattr(session, "transcript_path", None),
    )
    if transcript_path and transcript_path != getattr(session, "transcript_path", None):
        updated = manager._session_manager.update(
            session_id=session_id,
            transcript_path=transcript_path,
        )
        if updated is not None:
            session = updated
    raw_terminal_context = event.data.get("terminal_context")
    terminal_context = raw_terminal_context if isinstance(raw_terminal_context, dict) else None
    terminal_context = enrich_terminal_context_with_cwd(
        terminal_context,
        hook_cwd(event.data, event.cwd),
    )

    resolution = None
    if (
        session is not None
        and isinstance(project_id, str)
        and project_id
        and isinstance(machine_id, str)
        and machine_id
    ):
        try:
            resolution = resolve_matching_clear_continuation(
                handlers,
                machine_id=machine_id,
                project_id=project_id,
                cli_source=event.source.value,
                terminal_context=terminal_context,
            )
        except Exception as exc:
            handlers.logger.exception(
                "Clear continuation lookup failed during deferred activation: %s",
                exc,
            )

    additional_context = handlers._activate_materialized_session(
        event,
        session_id,
        resolution=resolution,
        session_obj=session,
        project_id=project_id,
        transcript_path=transcript_path,
        terminal_context=terminal_context,
    )
    context_mode = str(event.metadata.pop(_CONTEXT_MODE_METADATA_KEY, "live"))
    synthetic = build_synthetic_session_start(event, session_id)

    workflow_context, blocking_response = manager._evaluate_workflow_rules(
        synthetic,
        blocking_deadline,
    )
    if blocking_response is not None:
        return cast(
            HookResponse,
            manager._complete_response(
                synthetic,
                blocking_response,
                workflow_context,
                preserve_original=True,
            ),
        )

    webhook_block = manager._evaluate_blocking_webhooks(synthetic, blocking_deadline)
    if webhook_block is not None:
        return cast(
            HookResponse,
            manager._complete_response(
                synthetic,
                webhook_block,
                workflow_context,
                preserve_original=True,
            ),
        )

    if workflow_context:
        additional_context.append(workflow_context)

    clear_queued_context(handlers._session_manager, session_id)
    startup_response = handlers._compose_session_response(
        session=session,
        session_id=session_id,
        external_id=event.session_id,
        parent_session_id=getattr(session, "parent_session_id", None),
        machine_id=machine_id,
        project_id=project_id,
        task_id=event.task_id,
        additional_context=additional_context,
        terminal_context=terminal_context,
    )
    if event.event_type != HookEventType.BEFORE_AGENT:
        handlers._inject_agent_instructions_if_needed(event, session_id, startup_response)

    manager._dispatch_webhooks_async(synthetic, startup_response)
    event.metadata["_startup_context"] = startup_response.context
    event.metadata["_startup_system_message"] = startup_response.system_message
    if context_mode == "full":
        mark_startup_context_injected(handlers, session_id)
    return None
