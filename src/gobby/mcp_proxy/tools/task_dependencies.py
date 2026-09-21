"""
Task dependency MCP tools module.

Provides tools for managing task dependencies:
- add_dependency: Add a dependency between tasks
- remove_dependency: Remove a dependency
- get_dependency_tree: Get upstream/downstream dependencies
- check_dependency_cycles: Detect circular dependencies

Extracted from tasks.py using Strangler Fig pattern for code decomposition.
"""

from typing import TYPE_CHECKING, Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.tasks import TaskNotFoundError
from gobby.utils.project_context import get_project_context

if TYPE_CHECKING:
    from gobby.storage.task_dependencies import TaskDependencyManager
    from gobby.storage.tasks import LocalTaskManager

__all__ = ["create_dependency_registry"]


def get_current_project_id() -> str | None:
    """Get the current project ID from context."""
    context = get_project_context()
    return context.get("id") if context else None


def create_dependency_registry(
    task_manager: "LocalTaskManager | None" = None,
    dep_manager: "TaskDependencyManager | None" = None,
) -> InternalToolRegistry:
    """
    Create a registry with task dependency tools.

    Args:
        task_manager: LocalTaskManager instance (required for task ID resolution)
        dep_manager: TaskDependencyManager instance

    Returns:
        InternalToolRegistry with dependency tools registered
    """
    # Lazy import to avoid circular dependency
    from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp

    registry = InternalToolRegistry(
        name="gobby-tasks-dependencies",
        description="Task dependency management tools",
    )

    if dep_manager is None:
        raise ValueError("dep_manager is required")
    if task_manager is None:
        raise ValueError("task_manager is required for task ID resolution")

    # --- add_dependency ---

    def add_dependency(
        task_id: str,
        depends_on: str,
        dep_type: Literal["blocks", "discovered-from", "related"] = "blocks",
    ) -> dict[str, Any]:
        """Add a dependency between tasks."""
        # Resolve task references
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid task_id: {e}"}
        try:
            resolved_depends_on = resolve_task_id_for_mcp(task_manager, depends_on)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid depends_on: {e}"}

        try:
            dep_manager.add_dependency(resolved_task_id, resolved_depends_on, dep_type)
            return {
                "added": True,
                "task_id": resolved_task_id,
                "depends_on": resolved_depends_on,
                "dep_type": dep_type,
            }
        except ValueError as e:
            return {"error": str(e)}

    registry.register(
        name="add_dependency",
        description="Add a dependency between tasks. Creates a blocking relationship where depends_on must complete before task_id.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The dependent task (blocked by depends_on): #N, N (seq_num), path (1.2.3), or UUID",
                },
                "depends_on": {
                    "type": "string",
                    "description": "The blocker task (must complete first): #N, N (seq_num), path (1.2.3), or UUID",
                },
                "dep_type": {
                    "type": "string",
                    "description": 'Dependency type: "blocks" (default), "discovered-from", or "related"',
                    "default": "blocks",
                    "enum": ["blocks", "discovered-from", "related"],
                },
            },
            "required": ["task_id", "depends_on"],
        },
        func=add_dependency,
    )

    # --- remove_dependency ---

    def remove_dependency(task_id: str, depends_on: str) -> dict[str, Any]:
        """Remove a dependency between tasks."""
        # Resolve task references
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid task_id: {e}"}
        try:
            resolved_depends_on = resolve_task_id_for_mcp(task_manager, depends_on)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid depends_on: {e}"}

        try:
            dep_manager.remove_dependency(resolved_task_id, resolved_depends_on)
            return {"removed": True, "task_id": resolved_task_id, "depends_on": resolved_depends_on}
        except ValueError as e:
            return {"error": str(e)}

    registry.register(
        name="remove_dependency",
        description="Remove a dependency between tasks.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The dependent task: #N, N (seq_num), path (1.2.3), or UUID",
                },
                "depends_on": {
                    "type": "string",
                    "description": "The blocker task to unlink: #N, N (seq_num), path (1.2.3), or UUID",
                },
            },
            "required": ["task_id", "depends_on"],
        },
        func=remove_dependency,
    )

    # --- get_dependency_tree ---

    def get_dependency_tree(task_id: str, direction: str = "both") -> dict[str, Any]:
        """Get dependency tree for a task."""
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid task_id: {e}"}

        tree: dict[str, Any] = dep_manager.get_dependency_tree(resolved_task_id)
        if direction == "blockers":
            return {"blockers": tree.get("blockers", [])}
        elif direction == "blocking":
            return {"blocking": tree.get("blocking", [])}
        return tree

    registry.register(
        name="get_dependency_tree",
        description="Get dependency tree showing upstream blockers and downstream dependents.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Root task reference: #N, N (seq_num), path (1.2.3), or UUID",
                },
                "direction": {
                    "type": "string",
                    "description": '"blockers" (upstream), "blocking" (downstream), or "both"',
                    "default": "both",
                    "enum": ["blockers", "blocking", "both"],
                },
            },
            "required": ["task_id"],
        },
        func=get_dependency_tree,
        read_only=True,
    )

    # --- check_dependency_cycles ---

    def check_dependency_cycles() -> dict[str, Any]:
        """Detect circular dependencies in the project."""
        cycles = dep_manager.check_cycles()
        if cycles:
            return {"has_cycles": True, "cycles": cycles}
        return {"has_cycles": False}

    registry.register(
        name="check_dependency_cycles",
        description="Detect circular dependencies in the project. Returns any cycles found.",
        input_schema={"type": "object", "properties": {}},
        func=check_dependency_cycles,
        read_only=True,
    )

    return registry
