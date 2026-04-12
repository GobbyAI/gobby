"""Session response helper functions.

Standalone functions (not methods) that build session-start responses.
They accept the handler instance as the first ``handler`` parameter.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookResponse
from gobby.tasks.state_semantics import ACTIVE_CLAIM_STATUSES

if TYPE_CHECKING:
    from gobby.hooks.event_handlers._base import EventHandlersBase
    from gobby.hooks.event_handlers._session_start import AgentActivationResult
    from gobby.storage.session_models import Session

_logger = logging.getLogger(__name__)


def get_claimed_task_info(
    handler: EventHandlersBase,
    session_id: str | None,
    project_id: str | None,
) -> list[tuple[str, str, str]] | None:
    """Fetch claimed task details from session variables.

    Reads the ``claimed_tasks`` session variable (a dict of task UUIDs)
    and resolves each to its current ref, status, and title.

    Best-effort: returns None on any failure (mocked DB, missing tables, etc.)

    Returns:
        List of (ref, status, title) tuples, or None if no claimed tasks.
    """
    if not session_id or not handler._session_storage or not handler._task_manager:
        return None

    try:
        from gobby.workflows.state_manager import SessionVariableManager

        sv_mgr = SessionVariableManager(handler._session_storage.db)
        session_vars = sv_mgr.get_variables(session_id)
    except Exception as e:
        _logger.debug(f"Failed to load session variables for {session_id}: {e}")
        return None

    if not session_vars.get("task_claimed") or not session_vars.get("claimed_tasks"):
        # DB fallback: check for tasks still assigned to this session
        try:
            db_tasks = handler._task_manager.list_tasks(
                assignee=session_id,
                status=list(ACTIVE_CLAIM_STATUSES),
                project_id=project_id,
            )
            if db_tasks:
                reconciled = {}
                db_result: list[tuple[str, str, str]] = []
                for task in db_tasks:
                    ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]
                    reconciled[task.id] = ref
                    db_result.append((ref, task.status, task.title))
                # Reconcile session variables with DB state
                sv_mgr.set_variable(session_id, "task_claimed", True)
                sv_mgr.set_variable(session_id, "claimed_tasks", reconciled)
                return db_result or None
        except Exception as e:
            _logger.debug(f"Failed to reconcile claimed tasks from DB: {e}")
        return None

    claimed_tasks: dict[str, Any] = session_vars["claimed_tasks"]
    if not claimed_tasks:
        return None

    result: list[tuple[str, str, str]] = []
    for task_uuid in list(claimed_tasks):
        try:
            task = handler._task_manager.get_task(task_uuid, project_id=project_id)
            ref = f"#{task.seq_num}" if task.seq_num else task_uuid[:8]
            result.append((ref, task.status, task.title))
        except Exception as e:
            _logger.debug(f"Failed to fetch task {task_uuid[:8]}: {e}")
            result.append((task_uuid[:8], "unknown", "(deleted)"))
    return result or None


def build_claimed_task_context(
    handler: EventHandlersBase,
    session_id: str,
    project_id: str | None,
) -> str | None:
    """Build additional_context string for claimed tasks.

    Returns a formatted context block listing all tasks claimed by this
    session, or None if there are no claimed tasks.
    """
    info = get_claimed_task_info(handler, session_id, project_id)
    if not info:
        return None

    lines = ["\n## Claimed Tasks (Persisted)\n"]
    lines.append(
        "You have claimed the following tasks from a previous context. "
        "These tasks are still assigned to you.\n"
    )
    for ref, status, title in info:
        lines.append(f"- {ref} [{status}] {title}")
    return "\n".join(lines)


def compose_session_response(
    handler: EventHandlersBase,
    session: Session | None,
    session_id: str | None,
    external_id: str,
    parent_session_id: str | None,
    machine_id: str,
    project_id: str | None = None,
    task_id: str | None = None,
    additional_context: list[str] | None = None,
    is_pre_created: bool = False,
    terminal_context: dict[str, Any] | None = None,
    agent_info: AgentActivationResult | None = None,
    session_source: str | None = None,
    claimed_tasks_info: list[tuple[str, str, str]] | None = None,
) -> HookResponse:
    """Build HookResponse for session start.

    Shared helper that builds the system message, context, and metadata
    for both pre-created and newly-created sessions.

    Args:
        handler: The event handler mixin instance
        session: Session object (used for seq_num)
        session_id: Session ID
        external_id: External (CLI-native) session ID
        parent_session_id: Parent session ID if any
        machine_id: Machine ID
        project_id: Project ID
        task_id: Task ID if any
        additional_context: Additional context strings to append (e.g., task/skill context)
        is_pre_created: Whether this is a pre-created session
        terminal_context: Terminal context dict to add to metadata
        session_source: Session source (e.g., "clear", "compact", "startup") for handoff indicator
        claimed_tasks_info: Pre-fetched claimed task info from get_claimed_task_info()

    Returns:
        HookResponse with system_message, context, and metadata
    """
    # Build context_parts
    context_parts: list[str] = []
    if parent_session_id:
        context_parts.append(f"Parent session: {parent_session_id}")
    if additional_context:
        context_parts.extend(additional_context)

    # Compute session_ref from session object or fallback to session_id
    session_ref = session_id
    if session and session.seq_num:
        session_ref = f"#{session.seq_num}"

    # Build system message — session ID banner only (for terminal display).
    # Agent tree, external ID, and claimed tasks removed to reduce token waste.
    # Full metadata (external_id, machine_id, project_id, terminal) is injected
    # by the enricher on first hook via _first_hook_for_session flag.
    if session_ref != session_id:
        system_message = f"\nGobby Session ID: {session_ref} ({session_id})"
    else:
        system_message = f"\nGobby Session ID: {session_ref}"

    # Build metadata
    metadata: dict[str, Any] = {
        "session_id": session_id,
        "session_ref": session_ref,
        "parent_session_id": parent_session_id,
        "machine_id": machine_id,
        "project_id": project_id,
        "external_id": external_id,
        "task_id": task_id,
    }
    if is_pre_created:
        metadata["is_pre_created"] = True
    if terminal_context:
        # Only include non-null terminal values
        for key, value in terminal_context.items():
            if value is not None:
                metadata[f"terminal_{key}"] = value

    final_context = "\n".join(context_parts) if context_parts else None

    response = HookResponse(
        decision="allow",
        context=final_context,
        system_message=system_message,
        metadata=metadata,
    )
    handler._apply_debug_echo(response)
    return response
