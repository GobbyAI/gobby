"""
Internal MCP tools for Gobby Task System.

Exposes functionality for:
- Task CRUD (create, get, update, close, delete, list)
- Dependencies (add, remove, tree, cycles)
- Ready Work (ready lists, blocked lists)
- Session Integration (link, get)
- Git Sync (trigger sync, status)

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool, list_tools, get_tool_schema).
"""

from typing import Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import (
    LocalTaskManager,
    Task,
)
from gobby.sync.tasks import TaskSyncManager
from gobby.tasks.expansion import TaskExpander
from gobby.tasks.validation import TaskValidator
from gobby.utils.project_context import get_project_context
from gobby.utils.project_init import initialize_project


def create_task_registry(
    task_manager: LocalTaskManager,
    sync_manager: TaskSyncManager,
    task_expander: TaskExpander | None = None,
    task_validator: TaskValidator | None = None,
) -> InternalToolRegistry:
    """
    Create a task tool registry with all task-related tools.

    Args:
        task_manager: LocalTaskManager instance
        sync_manager: TaskSyncManager instance
        task_expander: TaskExpander instance (optional)
        task_validator: TaskValidator instance (optional)

    Returns:
        InternalToolRegistry with all task tools registered
    """
    registry = InternalToolRegistry(
        name="gobby-tasks",
        description="Task management - CRUD, dependencies, sync",
    )

    @registry.tool(
        name="expand_task",
        description="Expand a high-level task into smaller subtasks using AI.",
    )
    async def expand_task(
        task_id: str,
        context: str | None = None,
    ) -> list[Task]:
        """
        Expand a task into subtasks.

        Args:
            task_id: ID of the task to expand
            context: Additional context for expansion

        Returns:
            List of created subtasks
        """
        if not task_expander:
            raise RuntimeError("Task expansion is not enabled")

        task = task_manager.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        subtask_data = await task_expander.expand_task(
            task_id=task.id,
            title=task.title,
            description=task.description,
            context=context,
        )

        created_subtasks = []
        for data in subtask_data:
            # Create subtask
            subtask = task_manager.create_task(
                title=data["title"],
                description=data.get("description"),
                parent_task_id=task.id,
                project_id=task.project_id,
            )
            created_subtasks.append(subtask)

        return created_subtasks

    @registry.tool(
        name="validate_task",
        description="Validate if a task is completed according to its description.",
    )
    async def validate_task(
        task_id: str,
        changes_summary: str,
        context: str | None = None,
    ) -> dict[str, Any]:
        """
        Validate task completion.

        Args:
            task_id: ID of the task to validate
            changes_summary: Summary of changes made (files, diffs, etc.)
            context: Additional context for validation (optional)

        Returns:
            Validation result
        """
        if not task_validator:
            raise RuntimeError("Task validation is not enabled")

        task = task_manager.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        result = await task_validator.validate_task(
            task_id=task.id,
            title=task.title,
            original_instruction=task.original_instruction,
            changes_summary=changes_summary,
        )

        # Update task status if validated
        if result.status == "valid":
            task_manager.close_task(task.id, reason="Completed via validation")

        return {
            "is_valid": result.status == "valid",
            "feedback": result.feedback,
            "status": result.status,
        }

    # Helper managers
    dep_manager = TaskDependencyManager(task_manager.db)
    session_task_manager = SessionTaskManager(task_manager.db)

    # --- Task CRUD ---

    def create_task(
        title: str,
        description: str | None = None,
        priority: int = 2,
        task_type: str = "task",
        parent_task_id: str | None = None,
        blocks: list[str] | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new task in the current project."""
        # Get current project context which is required for task creation
        ctx = get_project_context()
        if ctx and ctx.get("id"):
            project_id = ctx["id"]
        else:
            init_result = initialize_project()
            project_id = init_result.project_id

        task = task_manager.create_task(
            project_id=project_id,
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            parent_task_id=parent_task_id,
            labels=labels,
        )

        # Handle 'blocks' argument if provided (syntactic sugar)
        if blocks:
            for blocked_id in blocks:
                dep_manager.add_dependency(task.id, blocked_id, "blocks")

        result: dict[str, Any] = task.to_dict()
        return result

    registry.register(
        name="create_task",
        description="Create a new task in the current project.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "description": {
                    "type": "string",
                    "description": "Detailed description",
                    "default": None,
                },
                "priority": {
                    "type": "integer",
                    "description": "Priority level (1=High, 2=Medium, 3=Low)",
                    "default": 2,
                },
                "task_type": {
                    "type": "string",
                    "description": "Task type (task, bug, feature, epic)",
                    "default": "task",
                },
                "parent_task_id": {
                    "type": "string",
                    "description": "Optional parent task ID",
                    "default": None,
                },
                "blocks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of task IDs that this new task blocks (optional)",
                    "default": None,
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of labels (optional)",
                    "default": None,
                },
            },
            "required": ["title"],
        },
        func=create_task,
    )

    def get_task(task_id: str) -> dict[str, Any]:
        """Get task details including dependencies."""
        task = task_manager.get_task(task_id)
        if not task:
            return {"error": f"Task {task_id} not found", "found": False}

        result: dict[str, Any] = task.to_dict()

        # Enrich with dependency info
        blockers = dep_manager.get_blockers(task_id)
        blocking = dep_manager.get_blocking(task_id)

        result["dependencies"] = {
            "blocked_by": [b.to_dict() for b in blockers],
            "blocking": [b.to_dict() for b in blocking],
        }

        return result

    registry.register(
        name="get_task",
        description="Get task details including dependencies.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The ID of the task to retrieve"},
            },
            "required": ["task_id"],
        },
        func=get_task,
    )

    def update_task(
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: int | None = None,
        assignee: str | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update task fields."""
        task = task_manager.update_task(
            task_id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            assignee=assignee,
            labels=labels,
        )
        if not task:
            return {"error": f"Task {task_id} not found"}
        result: dict[str, Any] = task.to_dict()
        return result

    registry.register(
        name="update_task",
        description="Update task fields.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "title": {"type": "string", "description": "New title", "default": None},
                "description": {
                    "type": "string",
                    "description": "New description",
                    "default": None,
                },
                "status": {
                    "type": "string",
                    "description": "New status (open, in_progress, closed)",
                    "default": None,
                },
                "priority": {"type": "integer", "description": "New priority", "default": None},
                "assignee": {"type": "string", "description": "New assignee", "default": None},
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New labels list",
                    "default": None,
                },
            },
            "required": ["task_id"],
        },
        func=update_task,
    )

    def add_label(task_id: str, label: str) -> dict[str, Any]:
        """Add a label to a task."""
        task = task_manager.add_label(task_id, label)
        if not task:
            return {"error": f"Task {task_id} not found"}
        result: dict[str, Any] = task.to_dict()
        return result

    registry.register(
        name="add_label",
        description="Add a label to a task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "label": {"type": "string", "description": "Label to add"},
            },
            "required": ["task_id", "label"],
        },
        func=add_label,
    )

    def remove_label(task_id: str, label: str) -> dict[str, Any]:
        """Remove a label from a task."""
        task = task_manager.remove_label(task_id, label)
        if not task:
            return {"error": f"Task {task_id} not found"}
        result: dict[str, Any] = task.to_dict()
        return result

    registry.register(
        name="remove_label",
        description="Remove a label from a task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "label": {"type": "string", "description": "Label to remove"},
            },
            "required": ["task_id", "label"],
        },
        func=remove_label,
    )

    def close_task(task_id: str, reason: str = "completed") -> dict[str, Any]:
        """Close a task with a reason."""
        task = task_manager.close_task(task_id)
        if not task:
            return {"error": f"Task {task_id} not found"}
        result: dict[str, Any] = task.to_dict()
        return result

    registry.register(
        name="close_task",
        description="Close a task with a reason.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "reason": {
                    "type": "string",
                    "description": 'Reason for closing (e.g., "completed", "wont_fix", "duplicate")',
                    "default": "completed",
                },
            },
            "required": ["task_id"],
        },
        func=close_task,
    )

    def delete_task(task_id: str, cascade: bool = False) -> dict[str, Any]:
        """Delete a task."""
        success = task_manager.delete_task(task_id, cascade=cascade)
        if not success:
            return {"error": f"Task {task_id} not found"}
        return {"success": True, "message": f"Task {task_id} deleted"}

    registry.register(
        name="delete_task",
        description="Delete a task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "cascade": {
                    "type": "boolean",
                    "description": "If True, delete all child tasks as well.",
                    "default": False,
                },
            },
            "required": ["task_id"],
        },
        func=delete_task,
    )

    def list_tasks(
        status: str | None = None,
        priority: int | None = None,
        task_type: str | None = None,
        assignee: str | None = None,
        label: str | None = None,
        parent_task_id: str | None = None,
        title_like: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List tasks with optional filters."""
        tasks = task_manager.list_tasks(
            status=status,
            priority=priority,
            task_type=task_type,
            assignee=assignee,
            label=label,
            parent_task_id=parent_task_id,
            title_like=title_like,
            limit=limit,
        )
        return {"tasks": [t.to_dict() for t in tasks], "count": len(tasks)}

    registry.register(
        name="list_tasks",
        description="List tasks with optional filters.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by status", "default": None},
                "priority": {
                    "type": "integer",
                    "description": "Filter by priority",
                    "default": None,
                },
                "task_type": {
                    "type": "string",
                    "description": "Filter by task type",
                    "default": None,
                },
                "assignee": {
                    "type": "string",
                    "description": "Filter by assignee",
                    "default": None,
                },
                "label": {
                    "type": "string",
                    "description": "Filter by label presence",
                    "default": None,
                },
                "parent_task_id": {
                    "type": "string",
                    "description": "Filter by parent task",
                    "default": None,
                },
                "title_like": {
                    "type": "string",
                    "description": "Filter by title (fuzzy match)",
                    "default": None,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of tasks to return",
                    "default": 50,
                },
            },
        },
        func=list_tasks,
    )

    # --- Dependencies ---

    def add_dependency(
        task_id: str,
        depends_on: str,
        dep_type: Literal["blocks", "discovered-from", "related"] = "blocks",
    ) -> dict[str, Any]:
        """Add a dependency between tasks."""
        try:
            dep_manager.add_dependency(task_id, depends_on, dep_type)
            return {"success": True, "message": f"Task {task_id} {dep_type} by {depends_on}"}
        except ValueError as e:
            return {"error": str(e)}

    registry.register(
        name="add_dependency",
        description="Add a dependency between tasks.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The dependent task (e.g., Task B)"},
                "depends_on": {"type": "string", "description": "The blocker task (e.g., Task A)"},
                "dep_type": {
                    "type": "string",
                    "description": 'Dependency type (default: "blocks")',
                    "default": "blocks",
                },
            },
            "required": ["task_id", "depends_on"],
        },
        func=add_dependency,
    )

    def remove_dependency(task_id: str, depends_on: str) -> dict[str, Any]:
        """Remove a dependency."""
        dep_manager.remove_dependency(task_id, depends_on)
        return {"success": True, "message": "Dependency removed"}

    registry.register(
        name="remove_dependency",
        description="Remove a dependency.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The dependent task"},
                "depends_on": {"type": "string", "description": "The blocker task"},
            },
            "required": ["task_id", "depends_on"],
        },
        func=remove_dependency,
    )

    def get_dependency_tree(task_id: str, direction: str = "both") -> dict[str, Any]:
        """Get dependency tree."""
        tree: dict[str, Any] = dep_manager.get_dependency_tree(task_id)
        if direction == "blockers":
            return {"blockers": tree.get("blockers", [])}
        elif direction == "blocking":
            return {"blocking": tree.get("blocking", [])}
        return tree

    registry.register(
        name="get_dependency_tree",
        description="Get dependency tree.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Root task ID"},
                "direction": {
                    "type": "string",
                    "description": '"blockers" (upstream), "blocking" (downstream), or "both"',
                    "default": "both",
                },
            },
            "required": ["task_id"],
        },
        func=get_dependency_tree,
    )

    def check_dependency_cycles() -> dict[str, Any]:
        """Detect circular dependencies in the project."""
        cycles = dep_manager.check_cycles()
        if cycles:
            return {"has_cycles": True, "cycles": cycles}
        return {"has_cycles": False}

    registry.register(
        name="check_dependency_cycles",
        description="Detect circular dependencies in the project.",
        input_schema={"type": "object", "properties": {}},
        func=check_dependency_cycles,
    )

    # --- Ready Work ---

    def list_ready_tasks(
        priority: int | None = None,
        task_type: str | None = None,
        assignee: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """List tasks that are open and have no unresolved blocking dependencies."""
        tasks = task_manager.list_ready_tasks(
            priority=priority,
            task_type=task_type,
            assignee=assignee,
            limit=limit,
        )
        return {"tasks": [t.to_dict() for t in tasks], "count": len(tasks)}

    registry.register(
        name="list_ready_tasks",
        description="List tasks that are open and have no unresolved blocking dependencies.",
        input_schema={
            "type": "object",
            "properties": {
                "priority": {
                    "type": "integer",
                    "description": "Filter by priority",
                    "default": None,
                },
                "task_type": {"type": "string", "description": "Filter by type", "default": None},
                "assignee": {
                    "type": "string",
                    "description": "Filter by assignee",
                    "default": None,
                },
                "limit": {"type": "integer", "description": "Max results", "default": 10},
            },
        },
        func=list_ready_tasks,
    )

    def list_blocked_tasks(limit: int = 20) -> dict[str, Any]:
        """List tasks that are currently blocked, including what blocks them."""
        blocked_items = task_manager.list_blocked_tasks(limit=limit)
        return {"blocked_tasks": blocked_items}

    registry.register(
        name="list_blocked_tasks",
        description="List tasks that are currently blocked, including what blocks them.",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max results", "default": 20},
            },
        },
        func=list_blocked_tasks,
    )

    # --- Session Integration ---

    def link_task_to_session(
        task_id: str,
        session_id: str | None = None,
        action: str = "worked_on",
    ) -> dict[str, Any]:
        """Link a task to a session."""
        if not session_id:
            return {"error": "session_id is required"}

        try:
            session_task_manager.link_task(session_id, task_id, action)
            return {"success": True, "message": f"Task {task_id} linked to session {session_id}"}
        except ValueError as e:
            return {"error": str(e)}

    registry.register(
        name="link_task_to_session",
        description="Link a task to a session.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "session_id": {
                    "type": "string",
                    "description": "Session ID (optional, defaults to linking context if available)",
                    "default": None,
                },
                "action": {
                    "type": "string",
                    "description": "Relationship type (worked_on, discovered, mentioned, closed)",
                    "default": "worked_on",
                },
            },
            "required": ["task_id"],
        },
        func=link_task_to_session,
    )

    def get_session_tasks(session_id: str) -> dict[str, Any]:
        """Get all tasks associated with a session."""
        tasks = session_task_manager.get_session_tasks(session_id)
        return {"session_id": session_id, "tasks": tasks}

    registry.register(
        name="get_session_tasks",
        description="Get all tasks associated with a session.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID"},
            },
            "required": ["session_id"],
        },
        func=get_session_tasks,
    )

    def get_task_sessions(task_id: str) -> dict[str, Any]:
        """Get all sessions that touched a task."""
        sessions = session_task_manager.get_task_sessions(task_id)
        return {"task_id": task_id, "sessions": sessions}

    registry.register(
        name="get_task_sessions",
        description="Get all sessions that touched a task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
            },
            "required": ["task_id"],
        },
        func=get_task_sessions,
    )

    # --- Git Sync ---

    def sync_tasks(direction: str = "both") -> dict[str, Any]:
        """Manually trigger task synchronization."""
        result = {}
        if direction in ["import", "both"]:
            sync_manager.import_from_jsonl()
            result["import"] = "completed"

        if direction in ["export", "both"]:
            sync_manager.export_to_jsonl()
            result["export"] = "completed"

        return result

    registry.register(
        name="sync_tasks",
        description="Manually trigger task synchronization.",
        input_schema={
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "description": '"import", "export", or "both"',
                    "default": "both",
                },
            },
        },
        func=sync_tasks,
    )

    def get_sync_status() -> dict[str, Any]:
        """Get current synchronization status."""
        result: dict[str, Any] = sync_manager.get_sync_status()
        return result

    registry.register(
        name="get_sync_status",
        description="Get current synchronization status.",
        input_schema={"type": "object", "properties": {}},
        func=get_sync_status,
    )

    return registry
