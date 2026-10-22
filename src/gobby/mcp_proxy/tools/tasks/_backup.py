"""Explicit task JSONL backup MCP tool. Restore is the operator's `gobby tasks restore`."""

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.sync.tasks import TaskBackupError, TaskBackupManager


def create_backup_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create the task backup tool."""
    registry = InternalToolRegistry(
        name="gobby-tasks-backup",
        description="Explicit task JSONL backup operations",
    )

    def backup_tasks() -> dict[str, Any]:
        """Write current project tasks to the machine-local JSONL backup."""
        project_id = ctx.get_current_project_id()
        manager = TaskBackupManager(ctx.task_manager)
        try:
            count = manager.backup(project_id=project_id)
        except TaskBackupError as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "backed_up": count}

    registry.register(
        name="backup_tasks",
        description=(
            "Write current live project tasks to the deterministic JSONL backup at "
            "~/.gobby/backups/<project-uuid>/tasks.jsonl."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        func=backup_tasks,
    )

    return registry
