"""Delete task handler for task lifecycle.

Handles the delete_task tool registration with cascade and unlink options.
"""

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._authorization import require_claim_authority
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskNotFoundError
from gobby.storage.tasks._models import TaskHasChildrenError, TaskHasDependentsError


def register_delete_task(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the delete_task tool on the given registry."""

    def delete_task(task_id: str, cascade: bool = False, unlink: bool = False) -> dict[str, Any]:
        """Delete a task.

        By default (cascade=False), fails if the task has children or dependents.
        Use cascade=True to delete subtasks and dependent tasks, or unlink=True
        to remove dependency links but preserve dependent tasks.
        """
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}

        # Get task before deleting to capture seq_num for ref
        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {task_id} not found"}
        ref = f"#{task.seq_num}" if task.seq_num else resolved_id

        denied = require_claim_authority(ctx.task_manager, task, "delete_task")
        if denied:
            return denied

        try:
            deleted = ctx.task_manager.delete_task(resolved_id, cascade=cascade, unlink=unlink)
            if not deleted:
                return {"error": f"Task {task_id} not found"}
        except TaskHasDependentsError as e:
            return {
                "success": False,
                "error": "has_dependents",
                "message": str(e),
                "suggestion": f"Use cascade=True to delete task {ref} and its dependents, "
                f"or unlink=True to preserve dependent tasks.",
            }
        except TaskHasChildrenError as e:
            return {
                "success": False,
                "error": "has_children",
                "message": str(e),
                "suggestion": f"Use cascade=True to delete task {ref} and all its subtasks.",
            }
        except ValueError as e:
            return {"error": str(e)}

        return {
            "success": True,
            "ref": ref,
            "deleted_task_id": resolved_id,
        }

    registry.register(
        name="delete_task",
        description="Delete a task. By default (cascade=False), fails if the task has children "
        "or dependents. Set cascade=True to delete subtasks and dependent tasks. "
        "Use unlink=True to remove dependency links but preserve dependent tasks.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "cascade": {
                    "type": "boolean",
                    "description": "If True, delete subtasks and dependent tasks. Defaults to False.",
                    "default": False,
                },
                "unlink": {
                    "type": "boolean",
                    "description": "If True, remove dependency links but preserve dependent tasks. "
                    "Ignored if cascade=True.",
                    "default": False,
                },
            },
            "required": ["task_id"],
        },
        func=delete_task,
    )
