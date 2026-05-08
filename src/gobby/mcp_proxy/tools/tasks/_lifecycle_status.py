"""Stage transition handlers for task lifecycle.

Handles reopen, escalate, and de_escalate tool registrations.
"""

import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._errors import TaskToolErrorCode, task_error
from gobby.mcp_proxy.tools.tasks._notifications import notify_parent_on_task_state_change
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.state_semantics import (
    get_claimed_session_id,
    projected_task_state,
)

logger = logging.getLogger(__name__)


def _state_error(message: str, state: str | None) -> dict[str, Any]:
    code = (
        TaskToolErrorCode.TASK_CLOSED
        if state == "closed"
        else TaskToolErrorCode.TASK_INVALID_STATUS
    )
    return task_error(message, code)


def _lifecycle_value_error(message: str) -> dict[str, Any]:
    if "closed" in message.lower():
        return _state_error(message, "closed")
    return {"error": message}


def _clear_prior_claim_session_variables(
    ctx: RegistryContext,
    task_id: str,
    prior_assignee: str | None,
    *,
    action: str,
) -> None:
    """Best-effort removal of a task from the prior owner's claimed task state."""
    if not prior_assignee:
        return

    try:
        from gobby.workflows.task_claim_state import remove_claimed_task

        session_vars = ctx.session_var_manager.get_variables(prior_assignee)
        merge_dict = remove_claimed_task(session_vars, task_id)
        ctx.session_var_manager.merge_variables(prior_assignee, merge_dict)
        logger.debug(
            "Removed task %s from claimed_tasks for session %s on %s",
            task_id,
            prior_assignee,
            action,
        )
    except Exception as e:
        logger.debug("Best-effort claimed_tasks cleanup on %s failed: %s", action, e)


def register_reopen_task(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the reopen_task tool on the given registry."""

    def reopen_task(task_id: str, reason: str | None = None) -> dict[str, Any]:
        """Reopen a closed or escalated task.

        Clears ownership, closure/escalation fields, and resets validation_fail_count.

        Args:
            task_id: Task reference (#N, path, or UUID)
            reason: Optional reason for reopening
        """
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}

        # Capture assignee before reopen clears it (needed for session variable cleanup)
        task = ctx.task_manager.get_task(resolved_id)
        prior_assignee = get_claimed_session_id(task) if task else None

        try:
            ctx.task_manager.reopen_task(resolved_id, reason=reason)

            _clear_prior_claim_session_variables(
                ctx,
                resolved_id,
                prior_assignee,
                action="reopen",
            )

            # Update session-task link to reflect reopen
            if prior_assignee:
                try:
                    ctx.session_task_manager.link_task(prior_assignee, resolved_id, "reopened")
                except Exception as e:
                    logger.debug(f"Best-effort session link update on reopen failed: {e}")

            return {}
        except ValueError as e:
            return {"error": str(e)}

    registry.register(
        name="reopen_task",
        description="Reopen a closed or escalated task. Clears ownership, closure/escalation fields, and resets validation. Optionally appends a reopen reason to the description.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference to reopen: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason for reopening the task",
                    "default": None,
                },
            },
            "required": ["task_id"],
        },
        func=reopen_task,
    )


def register_escalate_task(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the escalate_task tool on the given registry."""

    def escalate_task(
        task_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Escalate a task for human intervention.

        Sets escalation metadata with a reason and timestamp. Use when the
        task cannot be completed by the agent and needs human attention.

        Args:
            task_id: Task reference (#N, path, or UUID)
            reason: Why the task is being escalated

        Returns:
            Empty dict on success, or error dict with details.
        """
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return task_error(
                "No session context available. Ensure session_id is set.",
                TaskToolErrorCode.SESSION_REQUIRED,
            )

        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except TaskNotFoundError as e:
            return task_error(str(e), TaskToolErrorCode.TASK_NOT_FOUND)
        except ValueError as e:
            return {"error": str(e)}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return task_error(f"Task {task_id} not found", TaskToolErrorCode.TASK_NOT_FOUND)
        prior_assignee = get_claimed_session_id(task)

        projected_state = projected_task_state(task)
        if projected_state in {"escalated", "closed"}:
            return _state_error(
                f"Cannot escalate task in state '{projected_state}'.",
                projected_state,
            )

        try:
            ctx.task_manager.escalate_task(resolved_id, reason=reason)
        except ValueError as e:
            return _lifecycle_value_error(str(e))

        _clear_prior_claim_session_variables(
            ctx,
            resolved_id,
            prior_assignee,
            action="escalate",
        )

        notify_parent_on_task_state_change(
            ctx.task_manager.db,
            resolved_id,
            "escalated",
            task_ref=f"#{task.seq_num}" if task.seq_num else None,
        )

        # Link task to session (best-effort)
        if session_id:
            resolved_session_id = session_id
            try:
                resolved_session_id = ctx.resolve_session_id(session_id)
            except ValueError:
                pass
            try:
                ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "escalated")
            except Exception as e:
                logger.debug(f"Best-effort escalation linking failed: {e}")

        return {}

    registry.register(
        name="escalate_task",
        description="Escalate a task for human intervention. Sets escalation metadata and releases ownership.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "reason": {
                    "type": "string",
                    "description": "Why the task is being escalated (e.g., 'blocked by external dependency', 'needs architectural decision')",
                },
            },
            "required": ["task_id", "reason"],
        },
        func=escalate_task,
    )


def register_de_escalate_task(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the de_escalate_task tool on the given registry."""

    def de_escalate_task(
        task_id: str,
        reason: str,
        reset_validation: bool = False,
    ) -> dict[str, Any]:
        """De-escalate a task while preserving its current stage.

        Clears escalation metadata after human intervention resolves the issue.
        Optionally resets the validation failure count.

        Args:
            task_id: Task reference (#N, path, or UUID)
            reason: Reason for de-escalation (required)
            reset_validation: Also reset validation fail count (default: False)

        Returns:
            Empty dict on success, or error dict with details.
        """
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return task_error(
                "No session context available. Ensure session_id is set.",
                TaskToolErrorCode.SESSION_REQUIRED,
            )

        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except TaskNotFoundError as e:
            return task_error(
                f"Invalid task_id: {e}",
                TaskToolErrorCode.TASK_NOT_FOUND,
            )
        except ValueError as e:
            return {"error": f"Invalid task_id: {e}"}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return task_error(f"Task {task_id} not found", TaskToolErrorCode.TASK_NOT_FOUND)

        projected_state = projected_task_state(task)
        if projected_state != "escalated":
            return _state_error(
                f"Task {task_id} is not escalated (current state: {projected_state})",
                projected_state,
            )

        try:
            updated = ctx.task_manager.de_escalate_task(
                resolved_id,
                reason=reason,
                reset_validation=reset_validation,
            )
        except ValueError as e:
            return _lifecycle_value_error(str(e))

        if not updated:
            return {"error": f"Failed to de-escalate task {task_id}"}
        logger.info("Task %s de-escalated: %s", resolved_id, reason)

        notify_parent_on_task_state_change(
            ctx.task_manager.db,
            resolved_id,
            projected_task_state(updated),
            task_ref=f"#{task.seq_num}" if task.seq_num else None,
        )

        # Link task to session (best-effort)
        if session_id:
            resolved_session_id = session_id
            try:
                resolved_session_id = ctx.resolve_session_id(session_id)
            except ValueError:
                pass
            try:
                ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "de_escalated")
            except Exception as e:
                logger.debug(f"Best-effort de-escalation linking failed: {e}")

        return {}

    registry.register(
        name="de_escalate_task",
        description="Return an escalated task to its preserved current stage after human intervention resolves the issue. Optionally resets validation failure count.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "reason": {
                    "type": "string",
                    "description": "Why the task is being de-escalated (e.g., 'dependency resolved', 'workaround applied')",
                },
                "reset_validation": {
                    "type": "boolean",
                    "description": "Also reset the validation failure count (default: false)",
                    "default": False,
                },
            },
            "required": ["task_id", "reason"],
        },
        func=de_escalate_task,
    )
