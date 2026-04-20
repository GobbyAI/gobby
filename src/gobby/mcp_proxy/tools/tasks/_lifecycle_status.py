"""Status transition handlers for task lifecycle.

Handles reopen, escalate, de_escalate, mark_task_review_approved, and
mark_task_needs_review tool registrations.
"""

import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._notifications import notify_parent_on_status_change
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.state_semantics import get_claimed_session_id

logger = logging.getLogger(__name__)


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
        """Reopen a task to open status.

        Works on any non-open status. Clears assignee, closed fields,
        and resets validation_fail_count.

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

            # Reactivate any associated worktrees that were marked merged/abandoned
            try:
                from gobby.storage.worktrees import WorktreeStatus

                wt = ctx.worktree_manager.get_by_task(resolved_id)
                if wt and wt.status in (
                    WorktreeStatus.MERGED.value,
                    WorktreeStatus.ABANDONED.value,
                ):
                    ctx.worktree_manager.update(wt.id, status=WorktreeStatus.ACTIVE.value)
            except Exception as e:
                logger.debug(f"Best-effort reopen worktree update failed: {e}")

            return {}
        except ValueError as e:
            return {"error": str(e)}

    registry.register(
        name="reopen_task",
        description="Reopen a task to open status. Works on any non-open status. Clears assignee, closed fields, and resets validation. Optionally appends a reopen reason to the description.",
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

        Sets status to 'escalated' with a reason and timestamp. Use when
        the task cannot be completed by the agent and needs human attention.

        Args:
            task_id: Task reference (#N, path, or UUID)
            reason: Why the task is being escalated

        Returns:
            Empty dict on success, or error dict with details.
        """
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return {"error": "No session context available. Ensure session_id is set."}

        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {task_id} not found"}
        prior_assignee = get_claimed_session_id(task)

        if task.status in ("escalated", "closed"):
            return {"error": f"Cannot escalate task with status '{task.status}'."}

        try:
            ctx.task_manager.escalate_task(resolved_id, reason=reason)
        except ValueError as e:
            return {"error": str(e)}

        _clear_prior_claim_session_variables(
            ctx,
            resolved_id,
            prior_assignee,
            action="escalate",
        )

        notify_parent_on_status_change(
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
        description="Escalate a task for human intervention. Sets status to 'escalated'. Use when the task cannot be completed and needs human attention.",
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


def register_mark_task_review_approved(
    registry: InternalToolRegistry, ctx: RegistryContext
) -> None:
    """Register the mark_task_review_approved tool on the given registry."""

    def mark_task_review_approved(
        task_id: str,
        approval_notes: str | None = None,
    ) -> dict[str, Any]:
        """Approve a task after review.

        Sets status to 'review_approved', indicating the review gate has passed.
        Accepts tasks in 'needs_review', 'in_progress', or 'escalated' status.

        Args:
            task_id: Task reference (#N, path, or UUID)
            approval_notes: Optional notes about the approval

        Returns:
            Empty dict on success, or error dict with details.
        """
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return {"error": "No session context available. Ensure session_id is set."}

        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except TaskNotFoundError as e:
            return {"error": str(e)}
        except ValueError as e:
            return {"error": str(e)}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {task_id} not found"}
        prior_assignee = get_claimed_session_id(task)

        # Validate: current status must be needs_review, in_progress, or escalated
        if task.status not in ("needs_review", "in_progress", "escalated"):
            return {
                "error": f"Cannot approve task with status '{task.status}'. "
                "Task must be in 'needs_review', 'in_progress', or 'escalated' status to approve."
            }

        # Resolve session_id
        try:
            resolved_session_id = ctx.resolve_session_id(session_id)
        except ValueError as e:
            return {"error": f"Cannot resolve session '{session_id}': {e}"}

        # Best-effort: link commits before transitioning to review_approved.
        # QA agents may fix issues and commit before approving.
        try:
            from gobby.tasks.commits import auto_link_commits

            session = ctx.session_manager.get(resolved_session_id)
            repo_path = ctx.get_project_repo_path(task.project_id)
            if session:
                auto_link_commits(
                    task_manager=ctx.task_manager,
                    task_id=resolved_id,
                    since=session.created_at,
                    cwd=repo_path,
                    project_id=task.project_id,
                )
        except Exception:
            pass  # nosec B110 # best-effort, SESSION_END is the backstop

        try:
            updated = ctx.task_manager.mark_task_review_approved(
                resolved_id,
                approval_notes=approval_notes,
            )
        except ValueError as e:
            return {"error": str(e)}
        if not updated:
            return {"error": f"Failed to approve task {task_id}"}

        _clear_prior_claim_session_variables(
            ctx,
            resolved_id,
            prior_assignee,
            action="review approval",
        )

        notify_parent_on_status_change(
            ctx.task_manager.db,
            resolved_id,
            "review_approved",
            task_ref=f"#{task.seq_num}" if task.seq_num else None,
        )

        # Link task to session (best-effort)
        try:
            ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "review_approved")
        except Exception:
            pass  # nosec B110 # best-effort linking

        return {}

    registry.register(
        name="mark_task_review_approved",
        description="Approve a task after review. Sets status to 'review_approved' (review gate passed). Accepts tasks in 'needs_review', 'in_progress', or 'escalated' status.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "approval_notes": {
                    "type": "string",
                    "description": "Optional notes about the approval.",
                    "default": None,
                },
            },
            "required": ["task_id"],
        },
        func=mark_task_review_approved,
    )


def register_mark_task_needs_review(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the mark_task_needs_review tool on the given registry."""

    def mark_task_needs_review(
        task_id: str,
        review_notes: str | None = None,
    ) -> dict[str, Any]:
        """Mark a task as ready for review.

        Sets status to 'needs_review'. Use this when work is complete
        but needs human verification before closing.

        Args:
            task_id: Task reference (#N, path, or UUID)
            review_notes: Optional notes for the reviewer

        Returns:
            Empty dict on success, or error dict with details.
        """
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return {"error": "No session context available. Ensure session_id is set."}

        # Resolve task reference (supports #N, path, UUID formats)
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except TaskNotFoundError as e:
            return {"error": str(e)}
        except ValueError as e:
            return {"error": str(e)}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {task_id} not found"}
        prior_assignee = get_claimed_session_id(task)

        # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
        try:
            resolved_session_id = ctx.resolve_session_id(session_id)
        except ValueError as e:
            return {"error": f"Cannot resolve session '{session_id}': {e}"}

        # Best-effort: link commits before transitioning to needs_review.
        # Prevents the race where the orchestrator picks up the task
        # before SESSION_END auto-linking completes.
        try:
            from gobby.tasks.commits import auto_link_commits

            session = ctx.session_manager.get(resolved_session_id)
            repo_path = ctx.get_project_repo_path(task.project_id)
            if session:
                auto_link_commits(
                    task_manager=ctx.task_manager,
                    task_id=resolved_id,
                    since=session.created_at,
                    cwd=repo_path,
                    project_id=task.project_id,
                )
        except Exception:
            pass  # nosec B110 # best-effort, SESSION_END is the backstop

        try:
            updated = ctx.task_manager.mark_task_needs_review(
                resolved_id,
                review_notes=review_notes,
            )
        except ValueError as e:
            return {"error": str(e)}
        if not updated:
            return {"error": f"Failed to mark task {task_id} for review"}

        _clear_prior_claim_session_variables(
            ctx,
            resolved_id,
            prior_assignee,
            action="needs_review",
        )

        notify_parent_on_status_change(
            ctx.task_manager.db,
            resolved_id,
            "needs_review",
            task_ref=f"#{task.seq_num}" if task.seq_num else None,
        )

        # Link task to session (best-effort, don't fail if this fails)
        try:
            ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "needs_review")
        except Exception:
            pass  # nosec B110 # best-effort linking

        return {}

    registry.register(
        name="mark_task_needs_review",
        description="Mark a task as ready for review. Sets status to 'needs_review'. Use this when work is complete but needs human verification before closing.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "review_notes": {
                    "type": "string",
                    "description": "Optional notes for the reviewer explaining what was done and what to verify.",
                    "default": None,
                },
            },
            "required": ["task_id"],
        },
        func=mark_task_needs_review,
    )


def register_de_escalate_task(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the de_escalate_task tool on the given registry."""

    def de_escalate_task(
        task_id: str,
        reason: str,
        target_status: str | None = None,
        reset_validation: bool = False,
    ) -> dict[str, Any]:
        """De-escalate a task to an explicit next status.

        Returns an escalated task to the requested next state after human intervention resolves
        the issue. Optionally resets the validation failure count.

        Args:
            task_id: Task reference (#N, path, or UUID)
            reason: Reason for de-escalation (required)
            target_status: Where the task should return (default: open)
            reset_validation: Also reset validation fail count (default: False)

        Returns:
            Empty dict on success, or error dict with details.
        """
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return {"error": "No session context available. Ensure session_id is set."}

        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid task_id: {e}"}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {task_id} not found"}

        if task.status != "escalated":
            return {
                "error": f"Task {task_id} is not escalated (current status: {task.status})",
            }

        try:
            updated = ctx.task_manager.de_escalate_task(
                resolved_id,
                reason=reason,
                target_status=target_status,
                reset_validation=reset_validation,
            )
        except ValueError as e:
            return {"error": str(e)}

        if not updated:
            return {"error": f"Failed to de-escalate task {task_id}"}
        logger.info("Task %s de-escalated: %s", resolved_id, reason)

        notify_parent_on_status_change(
            ctx.task_manager.db,
            resolved_id,
            updated.status,
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
        description="Return an escalated task to an explicit next status after human intervention resolves the issue. Optionally resets validation failure count.",
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
                "target_status": {
                    "type": "string",
                    "enum": ["open", "in_progress", "needs_review", "review_approved"],
                    "description": "Status to return the task to after de-escalation (default: open)",
                    "default": "open",
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
