"""Explicit task JSONL backup and restore MCP tools."""

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.sync.tasks import TaskBackupManager, TaskRestoreError


def create_backup_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create task backup and restore tools."""
    registry = InternalToolRegistry(
        name="gobby-tasks-backup",
        description="Explicit task JSONL backup and restore operations",
    )

    def backup_tasks(output_path: str | None = None) -> dict[str, Any]:
        """Write current project tasks to a deterministic JSONL backup."""
        project_id = ctx.get_current_project_id()
        manager = TaskBackupManager(ctx.task_manager, backup_path=output_path)
        count = manager.backup(project_id=project_id)
        return {"success": True, "backed_up": count}

    registry.register(
        name="backup_tasks",
        description="Write current live project tasks to a deterministic JSONL backup.",
        input_schema={
            "type": "object",
            "properties": {
                "output_path": {
                    "type": "string",
                    "description": "Optional output path; defaults to .gobby/tasks.jsonl",
                }
            },
        },
        func=backup_tasks,
    )

    def restore_tasks(input_path: str | None = None) -> dict[str, Any]:
        """Non-destructively restore tasks from a JSONL backup."""
        project_id = ctx.get_current_project_id()
        manager = TaskBackupManager(ctx.task_manager, backup_path=input_path)
        try:
            count = manager.restore(project_id=project_id)
        except TaskRestoreError as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "restored": count}

    registry.register(
        name="restore_tasks",
        description="Restore task records when backup timestamps are newer.",
        input_schema={
            "type": "object",
            "properties": {
                "input_path": {
                    "type": "string",
                    "description": "Optional input path; defaults to .gobby/tasks.jsonl",
                }
            },
        },
        func=restore_tasks,
    )

    return registry
