"""Best-effort consumers of authoritative PostgreSQL task escalation state."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from gobby.mcp_proxy.tools.tasks._notifications import notify_parent_on_task_state_change

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.storage.tasks import Task

logger = logging.getLogger(__name__)


def derive_escalation_event_id(task_id: str, escalated_at: datetime | str) -> str:
    """Derive the stable event identity from the authoritative escalation row."""
    timestamp = escalated_at.isoformat() if isinstance(escalated_at, datetime) else escalated_at
    return f"task-escalated:{task_id}:{timestamp}"


def clear_prior_claim_session_variables(
    ctx: RegistryContext,
    task_id: str,
    prior_owner_session_id: str | None,
    *,
    action: str,
) -> None:
    """Best-effort release of a task from the prior owner's claimed task state.

    The claim goes; the edit attribution stays. Escalation pauses a task, so the
    prior owner is still the session that edited its files.
    """
    if not prior_owner_session_id:
        return

    try:
        from gobby.workflows.task_claim_state import release_claimed_task

        session_vars = ctx.session_var_manager.get_variables(prior_owner_session_id)
        merge_dict = release_claimed_task(session_vars, task_id)
        ctx.session_var_manager.merge_variables(prior_owner_session_id, merge_dict)
        logger.debug(
            "Released task %s from claimed_tasks for session %s on %s",
            task_id,
            prior_owner_session_id,
            action,
        )
    except Exception:
        logger.debug("Best-effort claimed_tasks cleanup on %s failed", action, exc_info=True)


def coordinate_task_escalation(
    ctx: RegistryContext,
    task: Task,
    *,
    prior_owner_session_id: str | None,
    session_id: str | None,
) -> str:
    """Fan out best-effort escalation side effects from an authoritative task row."""
    if task.escalated_at is None or not task.is_escalated:
        raise ValueError("Escalation coordination requires an authoritative escalated task row")

    event_id = derive_escalation_event_id(task.id, task.escalated_at)
    clear_prior_claim_session_variables(
        ctx,
        task.id,
        prior_owner_session_id,
        action="escalate",
    )

    try:
        notify_parent_on_task_state_change(
            ctx.task_manager.db,
            task.id,
            "escalated",
            task_ref=f"#{task.seq_num}" if task.seq_num else None,
            event_id=event_id,
        )
    except Exception:
        logger.debug("Best-effort escalation notification failed", exc_info=True)

    if session_id:
        resolved_session_id = session_id
        try:
            resolved_session_id = ctx.resolve_session_id(session_id)
        except ValueError:
            pass
        try:
            # SessionTaskManager.link_task is an ON CONFLICT upsert.
            ctx.session_task_manager.link_task(resolved_session_id, task.id, "escalated")
        except Exception:
            logger.debug("Best-effort escalation linking failed", exc_info=True)

    return event_id
