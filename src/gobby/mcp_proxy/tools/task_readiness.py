"""
Task readiness MCP tools module.

Provides tools for task readiness management:
- list_ready_tasks: List tasks with no unresolved blocking dependencies
- list_blocked_tasks: List tasks that are blocked by dependencies
- suggest_next_task: Suggest the best next task based on scoring

Extracted from tasks.py using Strangler Fig pattern for code decomposition.
"""

import logging
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.tasks import TaskNotFoundError
from gobby.utils.project_context import get_project_context
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)

__all__ = [
    "create_readiness_registry",
    "is_descendant_of",
    "_get_ancestry_chain",
    "_compute_proximity_boost",
]


def get_current_project_id() -> str | None:
    """Get the current project ID from context."""
    context = get_project_context()
    return context.get("id") if context else None


def _get_ready_descendants(
    task_manager: "LocalTaskManager",
    parent_task_id: str,
    task_type: str | None = None,
    project_id: str | None = None,
) -> list[Any]:
    """
    Get all ready tasks that are descendants of the given parent task.

    Traverses the task hierarchy to find all tasks under parent_task_id,
    then filters to only those that are ready (open with no blockers).

    Args:
        task_manager: LocalTaskManager instance
        parent_task_id: ID of the ancestor task to filter by
        task_type: Optional task type filter
        project_id: Optional project ID filter

    Returns:
        List of ready Task objects that are descendants of parent_task_id
    """
    # Get all ready tasks first
    all_ready = task_manager.list_ready_tasks(
        task_type=task_type,
        limit=200,  # Get more since we'll filter
        project_id=project_id,
    )

    if not all_ready:
        return []

    # Build a set of all descendant IDs by traversing the hierarchy
    descendant_ids = set()
    to_check = [parent_task_id]

    while to_check:
        current_id = to_check.pop()
        # Get direct children of this task
        children = task_manager.list_tasks(parent_task_id=current_id, limit=100)
        for child in children:
            if child.id not in descendant_ids:
                descendant_ids.add(child.id)
                to_check.append(child.id)

    # Filter ready tasks to only descendants
    return [t for t in all_ready if t.id in descendant_ids]


def is_descendant_of(
    task_manager: "LocalTaskManager",
    task_id: str,
    ancestor_id: str,
) -> bool:
    """
    Check if a task is a descendant of another task.

    Traverses up the parent chain from task_id to check if
    ancestor_id appears in the ancestry.

    Args:
        task_manager: LocalTaskManager instance
        task_id: ID of the potential descendant task
        ancestor_id: ID of the potential ancestor task

    Returns:
        True if task_id is a descendant of ancestor_id
    """
    if task_id == ancestor_id:
        return True  # A task is considered a descendant of itself

    current_id: str | None = task_id
    visited: set[str] = set()

    while current_id and current_id not in visited:
        visited.add(current_id)
        task = task_manager.get_task(current_id)
        if not task:
            return False
        if task.parent_task_id == ancestor_id:
            return True
        current_id = task.parent_task_id

    return False


def _get_ancestry_chain(
    task_manager: "LocalTaskManager",
    task_id: str,
) -> list[str]:
    """
    Build the ancestry chain for a task, from task up to root.

    Args:
        task_manager: LocalTaskManager instance
        task_id: ID of the task to get ancestry for

    Returns:
        List of task IDs starting with task_id and ending with root ancestor.
        Returns empty list if task doesn't exist.
    """
    chain: list[str] = []
    current_id: str | None = task_id
    visited: set[str] = set()

    while current_id and current_id not in visited:
        try:
            task = task_manager.get_task(current_id)
        except ValueError:
            # Task doesn't exist - return chain so far or empty if just started
            return chain if chain else []
        visited.add(current_id)
        chain.append(current_id)
        current_id = task.parent_task_id

    return chain


def _compute_proximity_boost(
    task_ancestry: list[str],
    active_ancestry: list[str],
) -> int:
    """
    Compute proximity boost based on common ancestry.

    The boost is higher for tasks closer to the active task in the hierarchy.
    - If task is a descendant of the active task: max boost (50)
    - Otherwise: max(0, 50 - (depth * 10)) where depth is distance to common ancestor

    Args:
        task_ancestry: Ancestry chain of the candidate task [task, parent, grandparent, ...]
        active_ancestry: Ancestry chain of the active (in_progress) task

    Returns:
        Proximity boost score (0-50)
    """
    if not task_ancestry or not active_ancestry:
        return 0

    # Convert to sets for O(1) lookup
    active_set = set(active_ancestry)
    active_task_id = active_ancestry[0]

    # Find first common ancestor and its depth from the task
    for depth, ancestor_id in enumerate(task_ancestry):
        if ancestor_id in active_set:
            # If common ancestor is the active task itself, task is a descendant
            # of active work - give max boost
            if ancestor_id == active_task_id:
                return 50
            # Otherwise, use depth from common ancestor
            return max(0, 50 - (depth * 10))

    # No common ancestor found
    return 0


def create_readiness_registry(
    task_manager: "LocalTaskManager | None" = None,
) -> InternalToolRegistry:
    """
    Create a registry with task readiness tools.

    Args:
        task_manager: LocalTaskManager instance (required)

    Returns:
        InternalToolRegistry with readiness tools registered
    """
    # Lazy import to avoid circular dependency
    from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp

    registry = InternalToolRegistry(
        name="gobby-tasks-readiness",
        description="Task readiness management tools",
    )

    if task_manager is None:
        raise ValueError("task_manager is required")

    # Create session variable manager for session_task scoping
    session_var_manager = SessionVariableManager(task_manager.db)
    session_manager = LocalSessionManager(task_manager.db)

    # --- list_ready_tasks ---

    def list_ready_tasks(
        priority: int | None = None,
        task_type: str | None = None,
        assignee: str | None = None,
        parent_task_id: str | None = None,
        limit: int = 10,
        all_projects: bool = False,
        project: str | None = None,
    ) -> dict[str, Any]:
        """List tasks that are open and have no unresolved blocking dependencies."""
        try:
            from gobby.mcp_proxy.tools.tasks._context import resolve_project_filter_standalone

            project_id = resolve_project_filter_standalone(project, all_projects, task_manager.db)
        except ValueError as e:
            return {"error": str(e), "tasks": [], "count": 0}

        # Resolve parent_task_id if it's a reference format
        if parent_task_id:
            try:
                parent_task_id = resolve_task_id_for_mcp(task_manager, parent_task_id, project_id)
            except (TaskNotFoundError, ValueError) as e:
                return {"error": f"Invalid parent_task_id: {e}", "tasks": [], "count": 0}

        tasks = task_manager.list_ready_tasks(
            priority=priority,
            task_type=task_type,
            assignee=assignee,
            parent_task_id=parent_task_id,
            limit=limit,
            project_id=project_id,
        )
        return {"tasks": [t.to_brief() for t in tasks], "count": len(tasks)}

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
                "parent_task_id": {
                    "type": "string",
                    "description": "Filter by parent task (find ready subtasks): #N, N (seq_num), path (1.2.3), or UUID",
                    "default": None,
                },
                "limit": {"type": "integer", "description": "Max results", "default": 10},
                "all_projects": {
                    "type": "boolean",
                    "description": "If true, list tasks from all projects instead of just the current project",
                    "default": False,
                },
                "project": {
                    "type": "string",
                    "description": "Filter by project name or UUID (e.g., '_personal')",
                    "default": None,
                },
            },
        },
        func=list_ready_tasks,
    )

    # --- list_blocked_tasks ---

    def list_blocked_tasks(
        parent_task_id: str | None = None,
        limit: int = 20,
        all_projects: bool = False,
        project: str | None = None,
    ) -> dict[str, Any]:
        """List tasks that are currently blocked, including what blocks them."""
        try:
            from gobby.mcp_proxy.tools.tasks._context import resolve_project_filter_standalone

            project_id = resolve_project_filter_standalone(project, all_projects, task_manager.db)
        except ValueError as e:
            return {"error": str(e), "tasks": [], "count": 0}

        # Resolve parent_task_id if it's a reference format
        if parent_task_id:
            try:
                parent_task_id = resolve_task_id_for_mcp(task_manager, parent_task_id, project_id)
            except (TaskNotFoundError, ValueError) as e:
                return {"error": f"Invalid parent_task_id: {e}", "tasks": [], "count": 0}

        blocked_tasks = task_manager.list_blocked_tasks(
            parent_task_id=parent_task_id,
            limit=limit,
            project_id=project_id,
        )
        return {"tasks": [t.to_brief() for t in blocked_tasks], "count": len(blocked_tasks)}

    registry.register(
        name="list_blocked_tasks",
        description="List tasks that are currently blocked by external dependencies (excludes parent tasks blocked by their own children).",
        input_schema={
            "type": "object",
            "properties": {
                "parent_task_id": {
                    "type": "string",
                    "description": "Filter by parent task (find blocked subtasks): #N, N (seq_num), path (1.2.3), or UUID",
                    "default": None,
                },
                "limit": {"type": "integer", "description": "Max results", "default": 20},
                "all_projects": {
                    "type": "boolean",
                    "description": "If true, list tasks from all projects instead of just the current project",
                    "default": False,
                },
                "project": {
                    "type": "string",
                    "description": "Filter by project name or UUID (e.g., '_personal')",
                    "default": None,
                },
            },
        },
        func=list_blocked_tasks,
    )

    # --- suggest_next_task ---

    def suggest_next_task(
        task_type: str | None = None,
        prefer_subtasks: bool = True,
        parent_task_id: str | None = None,
        session_id: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """
        Suggest the best next task to work on.

        Uses a scoring algorithm considering:
        - Task is ready (no blockers)
        - Priority (higher priority = higher score)
        - Is a leaf task (subtask with no children)
        - Has clear scope (complexity_score if available)
        - Proximity to current in_progress task (same branch preferred)

        Args:
            task_type: Filter by task type (optional)
            prefer_subtasks: Prefer leaf tasks over parent tasks (default: True)
            parent_task_id: Filter to descendants of this task (optional).
                      When set, only tasks under this parent hierarchy are considered.
                      Use this to explicitly scope suggestions to a specific epic/feature.
            session_id: Your session ID (required for MCP callers, from system context).
                       When provided and parent_task_id is not set, checks workflow state
                       for session_task variable and auto-scopes suggestions to that task's
                       hierarchy. Function signature is optional for TUI/internal callers.
            project: Filter by project name or UUID (optional).

        Returns:
            Suggested task with reasoning
        """
        # Filter by project
        try:
            from gobby.mcp_proxy.tools.tasks._context import resolve_project_filter_standalone

            project_id = resolve_project_filter_standalone(project, False, task_manager.db)
        except ValueError as e:
            return {"error": str(e), "suggestion": None}

        # Auto-scope to session_task if session_id is provided and parent_task_id is not set
        scoped_from_session_task = False
        if session_id and not parent_task_id:
            # Resolve session_id from #N format to UUID
            try:
                resolved_session_id = session_manager.resolve_session_reference(
                    session_id, project_id
                )
            except Exception as e:
                logger.warning(f"Could not resolve session_id '{session_id}': {e}")
                resolved_session_id = session_id

            session_vars = session_var_manager.get_variables(resolved_session_id)
            if session_vars:
                session_task = session_vars.get("session_task")
                if session_task and session_task != "*":
                    # session_task is set, use it as parent_task_id for scoping
                    parent_task_id = session_task
                    scoped_from_session_task = True

        # Resolve parent_task_id if it's a reference format
        if parent_task_id:
            try:
                parent_task_id = resolve_task_id_for_mcp(task_manager, parent_task_id, project_id)
            except (TaskNotFoundError, ValueError) as e:
                return {"error": f"Invalid parent_task_id: {e}", "suggestion": None}

        # If parent_task_id is set, get all descendants of that task
        if parent_task_id:
            ready_tasks = _get_ready_descendants(
                task_manager, parent_task_id, task_type, project_id
            )
            # If no ready descendants, check if the parent task itself is ready
            # This handles the case where session_task is a leaf task with no children
            if not ready_tasks:
                parent_task = task_manager.get_task(parent_task_id)
                if parent_task and parent_task.status == "open":
                    # Check if it matches task_type filter
                    if task_type is None or parent_task.task_type == task_type:
                        # Check if task is ready by seeing if it appears in ready list
                        ready_check = task_manager.list_ready_tasks(
                            project_id=project_id, limit=200
                        )
                        if any(t.id == parent_task_id for t in ready_check):
                            ready_tasks = [parent_task]
        else:
            ready_tasks = task_manager.list_ready_tasks(
                task_type=task_type, limit=50, project_id=project_id
            )

        if not ready_tasks:
            # If scoped from session_task and parent is closed, signal completion
            if scoped_from_session_task and parent_task_id:
                parent_task = task_manager.get_task(parent_task_id)
                if parent_task and parent_task.status == "closed":
                    return {
                        "suggestion": None,
                        "reason": "session_task is complete — all subtasks closed",
                        "session_task_complete": True,
                    }
            return {
                "suggestion": None,
                "reason": "No ready tasks found",
            }

        # Find current in_progress task for proximity scoring
        in_progress_tasks = task_manager.list_tasks(
            status="in_progress", limit=1, project_id=project_id
        )
        active_ancestry: list[str] = []
        active_task_id: str | None = None
        if in_progress_tasks:
            active_task_id = in_progress_tasks[0].id
            active_ancestry = _get_ancestry_chain(task_manager, active_task_id)

        # Filter out in_progress tasks - we want to suggest the NEXT task, not current
        ready_tasks = [t for t in ready_tasks if t.status != "in_progress"]
        if not ready_tasks:
            return {
                "suggestion": None,
                "reason": "No ready tasks found (all tasks are in_progress)",
            }

        # Score each task
        scored = []
        for task in ready_tasks:
            score = 0
            proximity_boost = 0

            # Priority boost - use weight of 110 per level to ensure priority dominates
            # This makes the gap between priority levels (110) larger than max possible
            # bonuses (leaf=25 + complexity=15 + category=10 + proximity=50 = 100)
            # Priority 0 (critical): 440, Priority 1 (high): 330, etc.
            score += (4 - task.priority) * 110

            # Check if it's a leaf task (no children)
            children = task_manager.list_tasks(parent_task_id=task.id, status="open", limit=1)
            is_leaf = len(children) == 0

            if prefer_subtasks and is_leaf:
                score += 25  # Prefer actionable leaf tasks

            # Bonus for tasks with clear complexity
            if task.complexity_score and task.complexity_score <= 5:
                score += 15  # Prefer lower complexity tasks

            # Bonus for tasks with category defined
            if task.category:
                score += 10

            # Proximity boost based on ancestry relationship to in_progress task
            if active_ancestry:
                task_ancestry = _get_ancestry_chain(task_manager, task.id)
                proximity_boost = _compute_proximity_boost(task_ancestry, active_ancestry)
                score += proximity_boost

            scored.append((task, score, is_leaf, proximity_boost))

        # Sort by score descending
        scored.sort(key=lambda x: x[1], reverse=True)
        best_task, best_score, is_leaf, best_proximity = scored[0]

        reasons = []
        if best_task.priority == 0:
            reasons.append("critical priority")
        elif best_task.priority == 1:
            reasons.append("high priority")
        if is_leaf:
            reasons.append("actionable leaf task")
        if best_task.complexity_score and best_task.complexity_score <= 5:
            reasons.append("manageable complexity")
        if best_task.category:
            reasons.append(f"has category ({best_task.category})")
        if best_proximity > 0:
            reasons.append("same branch as current work")

        # Get recommended skills based on task category
        recommended_skills: list[str] = []
        try:
            from gobby.skills.formatting import recommend_skills_for_task

            task_brief = best_task.to_brief()
            recommended_skills = recommend_skills_for_task(task_brief, db=task_manager.db)
        except Exception as e:
            logger.debug(f"Skill recommendation failed: {e}")

        return {
            "suggestion": best_task.to_brief(),
            "score": best_score,
            "reason": f"Selected because: {', '.join(reasons) if reasons else 'best available option'}",
            "alternatives": [
                {"ref": t.to_brief().get("ref", t.id), "title": t.title, "score": s}
                for t, s, _, _ in scored[1:4]  # Show top 3 alternatives
            ],
            "recommended_skills": recommended_skills,
        }

    registry.register(
        name="suggest_next_task",
        description="Suggest the best next task to work on based on priority, readiness, and complexity. "
        "Requires session_id to check workflow's session_task variable for automatic scoping. "
        "Use parent_task_id to explicitly scope suggestions to a specific epic/feature hierarchy.",
        input_schema={
            "type": "object",
            "properties": {
                "task_type": {
                    "type": "string",
                    "description": "Filter by task type (optional)",
                    "default": None,
                },
                "prefer_subtasks": {
                    "type": "boolean",
                    "description": "Prefer leaf tasks over parent tasks (default: True)",
                    "default": True,
                },
                "parent_task_id": {
                    "type": "string",
                    "description": "Filter to descendants of this task (#N, N, path, or UUID). "
                    "When set, only tasks under this parent hierarchy are considered. "
                    "Use this to scope suggestions to a specific epic/feature.",
                    "default": None,
                },
                "session_id": {
                    "type": "string",
                    "description": "Your session ID (from system context). When provided, auto-scopes suggestions based on workflow's session_task variable.",
                },
                "project": {
                    "type": "string",
                    "description": "Filter by project name or UUID (e.g., '_personal')",
                    "default": None,
                },
            },
        },
        func=suggest_next_task,
    )

    return registry
