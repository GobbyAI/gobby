"""Stage transition handlers for task lifecycle.

Handles reopen, escalate, and de_escalate tool registrations.
"""

import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._authorization import require_claim_authority
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._errors import TaskToolErrorCode, task_error
from gobby.mcp_proxy.tools.tasks._escalation_coordinator import (
    clear_prior_claim_session_variables,
    coordinate_task_escalation,
    link_escalating_session,
)
from gobby.mcp_proxy.tools.tasks._notifications import notify_parent_on_task_state_change
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskNotFoundError
from gobby.storage.tasks._de_escalation import REVIEW_REJECTION_LIMIT
from gobby.tasks.state_semantics import (
    get_claimed_session_id,
    projected_task_state,
)
from gobby.workflows.agent_resolver import session_has_capability_or_operator

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


def register_reopen_task(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the reopen_task tool on the given registry."""

    def reopen_task(task_id: str, reason: str | None = None) -> dict[str, Any]:
        """Reopen a closed or escalated task.

        Clears ownership and closure/escalation fields.

        Args:
            task_id: Task reference (#N, path, or UUID)
            reason: Optional reason for reopening
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
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}

        # Capture owner before reopen clears it for session variable cleanup.
        task = ctx.task_manager.get_task(resolved_id)
        prior_owner_session_id = get_claimed_session_id(task) if task else None

        if task is not None:
            denied = require_claim_authority(ctx.task_manager, task, "reopen_task")
            if denied:
                return denied

        try:
            ctx.task_manager.reopen_task(resolved_id, reason=reason)

            clear_prior_claim_session_variables(
                ctx,
                resolved_id,
                prior_owner_session_id,
                action="reopen",
            )

            # Update session-task link to reflect reopen
            if prior_owner_session_id:
                try:
                    ctx.session_task_manager.link_task(
                        prior_owner_session_id,
                        resolved_id,
                        "reopened",
                    )
                except ValueError:
                    raise
                except Exception as e:
                    logger.debug("Best-effort session link update on reopen failed: %s", e)

            # Attribute the reopen to the calling session (best-effort)
            try:
                resolved_session_id = ctx.resolve_session_id(session_id)
            except Exception as e:
                logger.debug("Best-effort reopen session resolution failed: %s", e)
            else:
                try:
                    ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "reopened")
                except ValueError:
                    raise
                except Exception as e:
                    logger.debug("Best-effort reopen attribution failed: %s", e)

            return {}
        except ValueError as e:
            return {"error": str(e)}

    registry.register(
        name="reopen_task",
        description="Reopen a closed or escalated task. Clears ownership and closure/escalation fields. Optionally appends a reopen reason to the description.",
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
        prior_owner_session_id = get_claimed_session_id(task)

        denied = require_claim_authority(ctx.task_manager, task, "escalate_task")
        if denied:
            return denied

        projected_state = projected_task_state(task)
        if projected_state == "escalated":
            # Escalated plus claimed is legal (override closes hold it), but it still
            # counts as the session's open claim. Escalating again releases that claim
            # and keeps the original escalation (#23204).
            if prior_owner_session_id and ctx.task_manager.release_escalated_task_claim(
                resolved_id, expected_owner=prior_owner_session_id
            ):
                clear_prior_claim_session_variables(
                    ctx, resolved_id, prior_owner_session_id, action="escalate"
                )
                link_escalating_session(ctx, resolved_id, session_id)
                return {}
            task_ref = f"#{task.seq_num}" if task.seq_num else resolved_id
            return _state_error(
                f"Cannot escalate task {task_ref}: it is already escalated and this session "
                "does not hold its claim. "
                f'Use de_escalate_task(task_id="{task_ref}", reason="<why it can resume>") '
                "to resume it.",
                projected_state,
            )
        if projected_state == "closed":
            return _state_error(
                f"Cannot escalate task in state '{projected_state}'.",
                projected_state,
            )

        try:
            escalated = ctx.task_manager.escalate_task(resolved_id, reason=reason)
        except ValueError as e:
            return _lifecycle_value_error(str(e))

        coordinate_task_escalation(
            ctx,
            escalated,
            prior_owner_session_id=prior_owner_session_id,
            session_id=session_id,
        )

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
    ) -> dict[str, Any]:
        """De-escalate a task and record the user's decision.

        Clears escalation metadata after human intervention resolves the issue.
        The claimant, task creator, or delegator can resolve escalation without
        releasing the task's claim.
        Optionally resets the validation failure count and current stage work attempts.
        Can restore a stopped approved stage from lifecycle history.

        Args:
            task_id: Task reference (#N, path, or UUID)
            reason: Reason for de-escalation (required)

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

        if session_id not in (
            task.created_in_session_id,
            task.delegated_by_session_id,
        ):
            denied = require_claim_authority(ctx.task_manager, task, "de_escalate_task")
            if denied:
                return denied

        projected_state = projected_task_state(task)
        if projected_state != "escalated":
            return _state_error(
                f"Task {task_id} is not escalated (current state: {projected_state})",
                projected_state,
            )

        if task.review_fail_count >= REVIEW_REJECTION_LIMIT:
            # Resetting an exhausted review requires a declared permission or the operator.
            try:
                caller = ctx.resolve_session_id(session_id)
            except ValueError:
                caller = None
            if caller is None or not session_has_capability_or_operator(
                ctx.task_manager.db, caller, "reset_review_epoch"
            ):
                return task_error(
                    f"Task {task_id} exhausted its review epoch; resetting requires the "
                    "reset_review_epoch capability or the operator's terminal.",
                    TaskToolErrorCode.REVIEW_RESET_FORBIDDEN,
                )

        try:
            updated = ctx.task_manager.de_escalate_task(
                resolved_id,
                reason=reason,
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
            except ValueError:
                raise
            except Exception as e:
                logger.debug("Best-effort de-escalation linking failed: %s", e)

        return {}

    registry.register(
        name="de_escalate_task",
        description=(
            "Return an escalated task to ready after human intervention resolves the issue. "
            "Resetting an exhausted review epoch requires the caller's enabled definition "
            "to declare reset_review_epoch, or the operator's own terminal."
        ),
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
            },
            "required": ["task_id", "reason"],
        },
        func=de_escalate_task,
    )
