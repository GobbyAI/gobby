"""Owner-controlled task path attribution release."""

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._errors import TaskToolErrorCode, task_error
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_closed
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.task_claim_state import normalize_task_edited_path


def register_release_task_paths(
    registry: InternalToolRegistry,
    ctx: RegistryContext,
) -> None:
    """Register the owner-only task path release tool."""

    def release_task_paths(task_id: str, paths: list[str]) -> dict[str, Any]:
        """Release committed or abandoned paths from the current session's task ledger."""
        session_ref = get_current_session_id()
        if not session_ref:
            return task_error(
                "No session context available. Ensure session_id is set.",
                TaskToolErrorCode.SESSION_REQUIRED,
            )

        try:
            resolved_task_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except TaskNotFoundError as exc:
            return task_error(str(exc), TaskToolErrorCode.TASK_NOT_FOUND)
        except ValueError as exc:
            return {"error": str(exc)}

        task = ctx.task_manager.get_task(resolved_task_id)
        if task is None:
            return task_error(
                f"Task {task_id} not found",
                TaskToolErrorCode.TASK_NOT_FOUND,
            )
        if is_task_closed(task):
            return task_error(
                f"Cannot release paths for {task_id}: task is closed",
                TaskToolErrorCode.TASK_CLOSED,
            )

        try:
            session_id = ctx.resolve_session_id(session_ref)
        except ValueError as exc:
            return {"error": f"Cannot resolve session '{session_ref}': {exc}"}

        owner_session_id = get_claimed_session_id(task)
        if owner_session_id != session_id:
            return {
                "error": "Only the task's owning session can release attributed paths",
                "task_id": resolved_task_id,
                "owner_session_id": owner_session_id,
                "session_id": session_id,
            }

        normalized_paths: list[str] = []
        for value in paths:
            path = normalize_task_edited_path(value)
            if path is None:
                return {"error": f"Invalid repository-relative path: {value!r}"}
            if path not in normalized_paths:
                normalized_paths.append(path)
        if not normalized_paths:
            return {"error": "paths must contain at least one repository-relative path"}

        released, remaining = ctx.session_var_manager.release_task_edited_files(
            session_id,
            resolved_task_id,
            normalized_paths,
        )
        return {
            "success": True,
            "task_id": resolved_task_id,
            "released_paths": released,
            "remaining_paths": remaining,
        }

    registry.register(
        name="release_task_paths",
        description=(
            "Release committed or abandoned paths from the current session's claimed task "
            "attribution. Use only after verifying the owning task has no uncommitted work "
            "on those paths."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Owned task reference: #N, path, or UUID",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Repository-relative paths to release",
                },
            },
            "required": ["task_id", "paths"],
        },
        func=release_task_paths,
    )


__all__ = ["register_release_task_paths"]
