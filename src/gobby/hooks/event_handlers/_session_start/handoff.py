"""Parent-session handoff helpers for session-start handling."""

from __future__ import annotations

import threading
import time
from typing import Any

from gobby.sessions.handoff_identity import terminal_context_matches_session
from gobby.tasks.state_semantics import (
    ACTIVE_STAGE_STATES,
    get_claimed_session_id,
    is_task_actionable,
)


def _compat_module() -> Any:
    import gobby.hooks.event_handlers._session_start as session_start

    return session_start


def find_parent_session(
    handler: Any,
    input_data: dict[str, Any],
    session_source: str,
    machine_id: str,
    project_id: str,
    cli_source: str,
) -> tuple[str | None, str]:
    """Find parent session and normalize the handoff source marker."""
    parent_session_id = input_data.get("parent_session_id")

    if (
        parent_session_id
        or not handler._session_manager
        or session_source not in ("clear", "compact")
    ):
        return parent_session_id, session_source

    try:
        parent = handler._session_manager.find_parent(
            machine_id=machine_id,
            project_id=project_id,
            source=cli_source,
            status="handoff_ready",
        )

        if not parent:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                time.sleep(0.3)
                parent = handler._session_manager.find_parent(
                    machine_id=machine_id,
                    project_id=project_id,
                    source=cli_source,
                    status="handoff_ready",
                )
                if parent:
                    handler.logger.debug(f"Found handoff_ready parent after backoff: {parent.id}")
                    break
            if not parent:
                handler.logger.warning(f"No handoff_ready parent found for /{session_source}")
                input_data["source"] = "startup"
                return None, "startup"

        if parent:
            child_terminal_context = input_data.get("terminal_context")
            if not terminal_context_matches_session(parent, child_terminal_context):
                handler.logger.warning(
                    "Ignoring %s handoff parent %s; terminal context does not match child",
                    session_source,
                    parent.id,
                )
                input_data["source"] = "startup"
                return None, "startup"

            parent_session_id = parent.id
            handler.logger.debug(f"Found parent session: {parent_session_id}")

            from gobby.workflows.state_manager import SessionVariableManager

            parent_vars = SessionVariableManager(handler._session_manager.db).get_variables(
                parent.id
            )
            handoff_source = parent_vars.get("handoff_source")
            if handoff_source in ("clear", "compact"):
                session_source = handoff_source
                input_data["source"] = session_source
    except Exception as e:
        handler.logger.warning(f"Error finding parent session: {e}")

    return parent_session_id, session_source


def populate_handoff_session_variables(
    handler: Any,
    session_id: str | None,
    parent_session_id: str | None,
    session_source: str,
) -> None:
    """Populate summary and task handoff variables for a new child session."""
    if not parent_session_id or not session_id or not handler._session_manager:
        return

    from gobby.workflows.state_manager import SessionVariableManager

    sv_mgr = SessionVariableManager(handler._session_manager.db)
    current_vars = sv_mgr.get_variables(session_id)
    if not current_vars.get("auto_inject_handoff", True):
        return

    parent = handler._session_manager.get(parent_session_id)
    if not parent:
        return

    if not parent.summary_markdown:
        summary_event = threading.Event()
        max_wait_s = _compat_module().SUMMARY_GENERATION_TIMEOUT_S
        dispatched = False
        if handler._dispatch_session_summaries_fn:
            try:
                handler._dispatch_session_summaries_fn(
                    parent_session_id,
                    True,
                    summary_event,
                    False,
                )
                dispatched = True
            except Exception as e:
                handler.logger.warning(
                    f"Failed to dispatch session summaries for parent {parent_session_id}: {e}"
                )

        if dispatched:
            if summary_event.wait(timeout=max_wait_s):
                handler.logger.debug(f"Session summary signaled for parent {parent_session_id}")
            else:
                handler.logger.warning(
                    "Timed out waiting for session summary for parent "
                    f"{parent_session_id} after {max_wait_s:.0f}s"
                )
        parent = handler._session_manager.get(parent_session_id)

    handoff_vars: dict[str, Any] = {}
    if parent and parent.summary_markdown:
        handoff_vars["session_summary"] = parent.summary_markdown
        handoff_vars["full_session_summary"] = parent.summary_markdown
    if handoff_vars:
        sv_mgr.merge_variables(session_id, handoff_vars)

    parent_vars = sv_mgr.get_variables(parent_session_id)
    if session_source in ("compact", "clear"):
        _preserve_task_claim_state(handler, sv_mgr, session_id, parent_session_id, parent_vars)


def _preserve_task_claim_state(
    handler: Any,
    sv_mgr: Any,
    session_id: str,
    parent_session_id: str,
    parent_vars: dict[str, Any],
) -> None:
    task_claim_keys = ("task_claimed", "claimed_tasks", "session_had_task")
    task_handoff = {k: parent_vars[k] for k in task_claim_keys if parent_vars.get(k)}
    if not task_handoff:
        return

    claimed_tasks = task_handoff.get("claimed_tasks") or {}
    merged_claims: dict[str, Any] = {}
    if task_handoff.get("session_had_task"):
        merged_claims["session_had_task"] = True

    filtered_claims: dict[str, str] = {}
    if task_handoff.get("task_claimed") and claimed_tasks:
        filtered_claims = _filter_and_reassign_claimed_tasks(
            handler,
            session_id,
            parent_session_id,
            claimed_tasks,
        )

    if filtered_claims:
        merged_claims["task_claimed"] = True
        merged_claims["claimed_tasks"] = filtered_claims
    if merged_claims:
        sv_mgr.merge_variables(session_id, merged_claims)


def _filter_and_reassign_claimed_tasks(
    handler: Any,
    session_id: str,
    parent_session_id: str,
    claimed_tasks: dict[str, str],
) -> dict[str, str]:
    filtered_claims: dict[str, str] = {}
    for claimed_id, claimed_ref in claimed_tasks.items():
        task_obj = None
        if handler._task_manager:
            try:
                task_obj = handler._task_manager.get_task(claimed_id)
            except Exception as e:
                handler.logger.debug(
                    "Best-effort task lookup failed for "
                    f"session={session_id} task={claimed_id}: {e}"
                )
                continue

        if task_obj is not None:
            legacy_status = getattr(task_obj, "status", None)
            if not is_task_actionable(task_obj) and legacy_status not in ACTIVE_STAGE_STATES:
                continue
            current_owner = get_claimed_session_id(task_obj)
            if current_owner not in (None, parent_session_id):
                handler.logger.debug(
                    "Skipping task handoff for session=%s task=%s; already assigned to %s",
                    session_id,
                    claimed_id,
                    current_owner,
                )
                continue
            if handler._task_manager is None:
                continue
            try:
                handler._task_manager.claim_task(
                    claimed_id,
                    session_id=session_id,
                    force=current_owner == parent_session_id,
                )
            except Exception as e:
                handler.logger.debug(
                    "Best-effort task re-assignment failed for "
                    f"session={session_id} task={claimed_id}: {e}"
                )
                continue

        filtered_claims[claimed_id] = claimed_ref
        if handler._session_task_manager:
            try:
                handler._session_task_manager.link_task(session_id, claimed_id, "claimed")
            except Exception as e:
                handler.logger.debug(
                    "Best-effort session-task link failed for "
                    f"session={session_id} task={claimed_id}: {e}"
                )
    return filtered_claims
