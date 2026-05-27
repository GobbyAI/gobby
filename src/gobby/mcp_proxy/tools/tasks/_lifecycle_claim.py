"""Claim task handler for task lifecycle.

Handles the claim_task tool registration including conflict detection,
session linking, and session variable management.
"""

import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._errors import TaskToolErrorCode, task_error
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskAlreadyClaimedError, TaskClosedError, TaskNotFoundError
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_closed
from gobby.workflows.claimed_task_skills import build_claimed_task_skill_state

logger = logging.getLogger(__name__)


def _has_delegated_agent_run(
    ctx: RegistryContext,
    *,
    child_session_id: str,
    task_id: str,
    current_owner: str | None,
) -> bool:
    """Return true when an active agent run proves parent-to-child delegation."""
    if not current_owner:
        return False

    try:
        row = ctx.task_manager.db.fetchone(
            """
            SELECT id FROM agent_runs
            WHERE child_session_id = ?
              AND task_id = ?
              AND parent_session_id = ?
              AND status IN ('pending', 'running')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (child_session_id, task_id, current_owner),
        )
    except Exception as e:
        logger.debug(f"Delegated claim lookup failed: {e}")
        return False

    try:
        run_id = row["id"] if row is not None else None
    except (KeyError, TypeError, IndexError):
        return False
    return isinstance(run_id, str) and bool(run_id)


def register_claim_task(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the claim_task tool on the given registry."""

    def claim_task(
        task_id: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """Claim a task for the current session.

        Sets the canonical owner and detects conflicts when another session
        has already claimed the task.

        Args:
            task_id: Task reference (#N, path, or UUID)
            force: Override existing claim by another session (default: False)

        Returns:
            Empty dict on success, or error dict with conflict information.
        """
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return task_error(
                "No session context available. Ensure session_id is set.",
                TaskToolErrorCode.SESSION_REQUIRED,
            )

        # Resolve task reference (supports #N, path, UUID formats)
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except TaskNotFoundError as e:
            return task_error(str(e), TaskToolErrorCode.TASK_NOT_FOUND)
        except ValueError as e:
            return {"error": str(e)}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return task_error(f"Task {task_id} not found", TaskToolErrorCode.TASK_NOT_FOUND)

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
        if is_task_closed(task):
            return task_error(
                f"Cannot claim task {resolved_id}: task is closed",
                TaskToolErrorCode.TASK_CLOSED,
            )

        current_owner = get_claimed_session_id(task)
        delegated_claim = False
        if current_owner and current_owner != resolved_session_id and not force:
            delegated_claim = _has_delegated_agent_run(
                ctx,
                child_session_id=resolved_session_id,
                task_id=resolved_id,
                current_owner=current_owner,
            )

        if (
            current_owner
            and current_owner != resolved_session_id
            and not force
            and not delegated_claim
        ):
            return task_error(
                "Task already claimed by another session",
                TaskToolErrorCode.TASK_CLAIM_CONFLICT,
                claimed_by=current_owner,
                message=(
                    f"Task is already claimed by session '{current_owner}'. "
                    "Use force=True to override."
                ),
            )

        try:
            updated = ctx.task_manager.claim_task(
                resolved_id,
                session_id=resolved_session_id,
                force=force or delegated_claim,
            )
        except TaskClosedError as e:
            return task_error(str(e), TaskToolErrorCode.TASK_CLOSED)
        except TaskAlreadyClaimedError as e:
            return task_error(
                "Task already claimed by another session",
                TaskToolErrorCode.TASK_CLAIM_CONFLICT,
                claimed_by=e.claimed_by,
                message=(
                    f"Task is already claimed by session '{e.claimed_by}'. "
                    "Use force=True to override."
                ),
            )
        except ValueError as e:
            return {"error": str(e)}

        if not updated:
            return {"error": f"Failed to claim task {task_id}"}

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
            current_vars = {**session_vars, **merge_dict}
            merge_dict.update(build_claimed_task_skill_state(current_vars, ctx.task_manager))
            ctx.session_var_manager.merge_variables(resolved_session_id, merge_dict)
        except Exception as e:
            logger.debug(f"Best-effort session variable setting failed: {e}")

        return {"success": True, "task_id": resolved_id}

    registry.register(
        name="claim_task",
        description="Claim a task for your session. Sets canonical ownership and detects conflicts if already claimed by another session.",
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
