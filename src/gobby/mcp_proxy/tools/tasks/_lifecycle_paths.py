"""Owner-controlled task path attribution release."""

import os
import subprocess
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._errors import TaskToolErrorCode, task_error
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_closed
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.task_claim_state import normalize_task_edited_path


def _dirty_repo_paths(repo_path: str, paths: list[str]) -> list[str]:
    result = subprocess.run(  # Hardcoded git command. # nosec B603 B607
        ["git", "--literal-pathspecs", "status", "--porcelain", "-z", "--", *paths],
        cwd=Path(repo_path),
        check=False,
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        stderr = os.fsdecode(result.stderr).strip()
        raise RuntimeError(f"git status failed: {stderr}")

    dirty: set[str] = set()
    records = iter(result.stdout.split(b"\0"))
    for record in records:
        if len(record) < 4:
            continue
        status = record[:2]
        path = normalize_task_edited_path(os.fsdecode(record[3:]))
        if path is not None:
            dirty.add(path)
        if b"R" in status or b"C" in status:
            original = normalize_task_edited_path(os.fsdecode(next(records, b"")))
            if original is not None:
                dirty.add(original)

    return [path for path in paths if path in dirty]


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
            return task_error(str(exc), TaskToolErrorCode.TASK_NOT_FOUND)

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
            return task_error(
                f"Cannot resolve session '{session_ref}': {exc}",
                TaskToolErrorCode.SESSION_REQUIRED,
            )

        owner_session_id = get_claimed_session_id(task)
        if owner_session_id != session_id:
            return task_error(
                "Only the task's owning session can release attributed paths",
                TaskToolErrorCode.TASK_CLAIM_CONFLICT,
                task_id=resolved_task_id,
                owner_session_id=owner_session_id,
                session_id=session_id,
            )

        normalized_paths: list[str] = []
        for value in paths:
            path = normalize_task_edited_path(value)
            if path is None:
                return task_error(
                    f"Invalid repository-relative path: {value!r}",
                    TaskToolErrorCode.TASK_INVALID_STATUS,
                )
            if path not in normalized_paths:
                normalized_paths.append(path)
        if not normalized_paths:
            return task_error(
                "paths must contain at least one repository-relative path",
                TaskToolErrorCode.TASK_INVALID_STATUS,
            )

        artifacts = ctx.task_manager.artifacts.get_artifacts(resolved_task_id)
        repo_path = artifacts.worktree_path or ctx.get_project_repo_path(task.project_id)
        if repo_path is None:
            return task_error(
                "Cannot verify task paths because the project has no repository path",
                TaskToolErrorCode.TASK_INVALID_STATUS,
            )
        try:
            dirty_paths = _dirty_repo_paths(repo_path, normalized_paths)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return task_error(
                f"Cannot verify task paths: {exc}",
                TaskToolErrorCode.TASK_INVALID_STATUS,
            )
        if dirty_paths:
            return task_error(
                "Cannot release paths with uncommitted content",
                TaskToolErrorCode.TASK_INVALID_STATUS,
                dirty_paths=dirty_paths,
            )

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
