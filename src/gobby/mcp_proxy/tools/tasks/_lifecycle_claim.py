"""Claim task handler for task lifecycle.

Handles the claim_task tool registration including conflict detection,
session linking, and session variable management.
"""

import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._notifications import notify_parent_on_status_change
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskNotFoundError

logger = logging.getLogger(__name__)


def register_claim_task(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the claim_task tool on the given registry."""

    def claim_task(
        task_id: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """Claim a task for the current session.

        Combines setting the assignee and marking as in_progress in a single
        atomic operation. Detects conflicts when another session has already
        claimed the task.

        Args:
            task_id: Task reference (#N, path, or UUID)
            force: Override existing claim by another session (default: False)

        Returns:
            Empty dict on success, or error dict with conflict information.
        """
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return {"error": "No session context available. Ensure session_id is set."}

        # Resolve task reference (supports #N, path, UUID formats)
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {task_id} not found"}

        # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
        try:
            resolved_session_id = ctx.resolve_session_id(session_id)
        except ValueError as e:
            return {"error": f"Cannot resolve session '{session_id}': {e}"}

        # Block cross-project claiming
        try:
            session = ctx.session_manager.get(resolved_session_id)
        except Exception:
            session = None
        if session and task.project_id != session.project_id:
            return {
                "error": "Cannot claim a task from a different project",
                "task_project": task.project_id,
                "session_project": session.project_id,
            }

        # Check if already claimed by another session
        if task.assignee and task.assignee != resolved_session_id and not force:
            return {
                "success": False,
                "error": "Task already claimed by another session",
                "claimed_by": task.assignee,
                "message": f"Task is already claimed by session '{task.assignee}'. Use force=True to override.",
            }

        # Update task: only transition to in_progress when task is open.
        # For other statuses (e.g. needs_review claimed by QA), preserve status.
        update_kwargs: dict[str, Any] = {"assignee": resolved_session_id}
        if task.status == "open":
            update_kwargs["status"] = "in_progress"
        updated = ctx.task_manager.update_task(
            resolved_id,
            **update_kwargs,
        )
        if not updated:
            return {"error": f"Failed to claim task {task_id}"}

        new_status = update_kwargs.get("status", task.status)
        if new_status != task.status:
            notify_parent_on_status_change(
                ctx.task_manager.db,
                resolved_id,
                new_status,
                task_ref=f"#{task.seq_num}" if task.seq_num else None,
            )

        # Link task to session (best-effort, don't fail the claim if this fails)
        try:
            ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "claimed")
        except Exception as e:
            logger.debug(f"Best-effort session claim linking failed: {e}")

        # Set claimed_tasks session variable (enables Edit/Write hooks)
        # This mirrors create_task behavior in _crud.py
        try:
            from gobby.workflows.task_claim_state import add_claimed_task

            session_vars = ctx.session_var_manager.get_variables(resolved_session_id)
            ref = f"#{task.seq_num}" if task.seq_num else resolved_id
            merge_dict = add_claimed_task(session_vars, resolved_id, ref)
            ctx.session_var_manager.merge_variables(resolved_session_id, merge_dict)
        except Exception as e:
            logger.debug(f"Best-effort session variable setting failed: {e}")

        return {"success": True, "task_id": resolved_id}

    registry.register(
        name="claim_task",
        description="Claim a task for your session. Sets assignee to session_id and status to in_progress. Detects conflicts if already claimed by another session.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "force": {
                    "type": "boolean",
                    "description": "Override existing claim by another session (default: False)",
                    "default": False,
                },
            },
            "required": ["task_id"],
        },
        func=claim_task,
    )
