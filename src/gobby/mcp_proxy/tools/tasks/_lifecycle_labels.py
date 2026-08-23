"""Label management handlers for task lifecycle.

Handles add_label and remove_label tool registrations.
"""

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._authorization import require_claim_authority
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._live_session_label import live_session_label_change_error
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskNotFoundError


def register_add_label(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the add_label tool on the given registry."""

    def add_label(task_id: str, label: str) -> dict[str, Any]:
        """Add a label to a task."""
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}
        current_task = ctx.task_manager.get_task(resolved_id)
        if current_task is None:
            return {"error": f"Task {task_id} not found"}
        denied = require_claim_authority(ctx.task_manager, current_task, "add_label")
        if denied:
            return denied
        label_error = live_session_label_change_error(
            ctx,
            current_task.labels,
            [*(current_task.labels or ()), label],
        )
        if label_error:
            return {"error": label_error}
        task = ctx.task_manager.add_label(resolved_id, label)
        if not task:
            return {"error": f"Task {task_id} not found"}
        return {"success": True, "task_id": resolved_id}

    registry.register(
        name="add_label",
        description="Add a label to a task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "label": {"type": "string", "description": "Label to add"},
            },
            "required": ["task_id", "label"],
        },
        func=add_label,
    )


def register_remove_label(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the remove_label tool on the given registry."""

    def remove_label(task_id: str, label: str) -> dict[str, Any]:
        """Remove a label from a task."""
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}
        current_task = ctx.task_manager.get_task(resolved_id)
        if current_task is None:
            return {"error": f"Task {task_id} not found"}
        denied = require_claim_authority(ctx.task_manager, current_task, "remove_label")
        if denied:
            return denied
        next_labels = [item for item in current_task.labels or () if item != label]
        label_error = live_session_label_change_error(ctx, current_task.labels, next_labels)
        if label_error:
            return {"error": label_error}
        task = ctx.task_manager.remove_label(resolved_id, label)
        if not task:
            return {"error": f"Task {task_id} not found"}
        return {"success": True, "task_id": resolved_id}

    registry.register(
        name="remove_label",
        description="Remove a label from a task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "label": {"type": "string", "description": "Label to remove"},
            },
            "required": ["task_id", "label"],
        },
        func=remove_label,
    )
