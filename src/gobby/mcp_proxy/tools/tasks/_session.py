"""Session integration tools for task management.

Provides tools for linking tasks to sessions and querying task-session
relationships.
"""

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._authorization import has_delegated_agent_run
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._errors import TaskToolErrorCode, task_error
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskNotFoundError


def create_session_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create a registry with session-task integration tools.

    Args:
        ctx: Shared registry context

    Returns:
        InternalToolRegistry with session tools registered
    """
    registry = InternalToolRegistry(
        name="gobby-tasks-session",
        description="Task-session integration tools",
    )

    def link_task_to_session(
        task_id: str,
        action: str = "worked_on",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Link a task to an explicit session or the current session."""
        from gobby.utils.session_context import get_current_session_id

        effective_session_id = session_id if session_id is not None else get_current_session_id()
        if not effective_session_id:
            return {"error": "No session context available. Ensure session_id is set."}

        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}

        # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
        try:
            resolved_session_id = ctx.resolve_session_id(effective_session_id)
        except ValueError as e:
            return {"error": f"Invalid session_id '{effective_session_id}': {e}"}

        # An explicit session_id naming another session is allowed only for
        # self or the task's agent-run delegation lineage (#20821).
        caller_session_id = get_current_session_id()
        if (
            session_id is not None
            and caller_session_id
            and resolved_session_id != caller_session_id
            and not has_delegated_agent_run(
                ctx.task_manager.db,
                caller_session_id=caller_session_id,
                task_id=resolved_id,
                owner_session_id=resolved_session_id,
            )
        ):
            return task_error(
                f"Cannot link task to session '{session_id}': linking another "
                "session requires agent-run delegation lineage with that session "
                "for this task.",
                TaskToolErrorCode.TASK_CLAIM_CONFLICT,
                target_session=resolved_session_id,
            )

        try:
            ctx.session_task_manager.link_task(resolved_session_id, resolved_id, action)
            return {}
        except ValueError as e:
            return {"error": str(e)}

    registry.register(
        name="link_task_to_session",
        description="Link a task to a session. Accepts #N, N, UUID, or prefix for session_id.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "session_id": {
                    "type": "string",
                    "description": "Session reference (accepts #N, N, UUID, or prefix)",
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

    def get_session_tasks(session_id: str | None = None) -> dict[str, Any]:
        """Get all tasks associated with a session."""
        from gobby.utils.session_context import get_current_session_id

        effective_session_id = session_id or get_current_session_id()
        if not effective_session_id:
            return {"error": "No session_id provided and no session context available."}
        # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
        try:
            resolved_session_id = ctx.resolve_session_id(effective_session_id)
        except ValueError as e:
            return {"error": f"Invalid session_id '{effective_session_id}': {e}"}

        tasks = ctx.session_task_manager.get_session_tasks(resolved_session_id)
        return {"session_id": resolved_session_id, "tasks": tasks}

    registry.register(
        name="get_session_tasks",
        description="Get all tasks associated with a session. Defaults to current session. Accepts #N, N, UUID, or prefix for session_id.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session reference (accepts #N, N, UUID, or prefix). Defaults to current session.",
                    "default": None,
                },
            },
        },
        func=get_session_tasks,
    )

    def get_task_sessions(task_id: str) -> dict[str, Any]:
        """Get all sessions that touched a task."""
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}
        task = ctx.task_manager.get_task(resolved_id)
        sessions = ctx.session_task_manager.get_task_sessions(resolved_id)
        # Handle case where task is not found (shouldn't happen after resolve, but be defensive)
        ref = f"#{task.seq_num}" if task and task.seq_num else resolved_id
        return {
            "ref": ref,
            "task_id": resolved_id,
            "sessions": sessions,
        }

    registry.register(
        name="get_task_sessions",
        description="Get all sessions that touched a task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
            },
            "required": ["task_id"],
        },
        func=get_task_sessions,
    )

    return registry
