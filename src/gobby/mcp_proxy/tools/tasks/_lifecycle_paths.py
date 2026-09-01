"""Owner-controlled task path attribution release."""

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._errors import TaskToolErrorCode, task_error
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.project_checkouts import OverlayRegistrationRejectedError, resolve_operation_root
from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_closed
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.commit_guard import (
    DirtyEditOwnershipInspectionError,
    foreign_owned_dirty_paths,
    inspect_checkout_path_ownership,
)
from gobby.workflows.task_claim_state import (
    normalize_task_edited_path,
    task_edited_file_times,
)

logger = logging.getLogger(__name__)


def _claimed_session_worktree_path(
    ctx: RegistryContext,
    *,
    session_id: str,
    project_id: str,
) -> str | None:
    worktrees = ctx.worktree_manager.list_worktrees(
        project_id=project_id,
        status="active",
        agent_session_id=session_id,
        limit=2,
    )
    if len(worktrees) > 1:
        raise ValueError("Session owns multiple active worktrees; release one before inspection")
    return worktrees[0].worktree_path if worktrees else None


def _lifecycle_checkout_root(
    ctx: RegistryContext,
    *,
    session_id: str,
    project_id: str,
    overlay_path: str | None,
) -> str | None:
    session = ctx.session_manager.get(session_id)
    machine_id = ctx.checkout_machine_id(project_id, session.id if session is not None else None)
    if overlay_path:
        try:
            return resolve_operation_root(
                ctx.task_manager.db,
                project_id,
                machine_id,
                overlay_path=overlay_path,
            )
        except OverlayRegistrationRejectedError:
            pass
    return ctx.get_project_repo_path(project_id, machine_id)


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


def _last_commit_epoch(repo_path: str, path: str) -> int:
    """Return the committer epoch of the last commit touching ``path`` (0 if none)."""
    result = subprocess.run(  # Hardcoded git command. # nosec B603 B607
        ["git", "--literal-pathspecs", "log", "-1", "--format=%ct", "--", path],
        cwd=Path(repo_path),
        check=False,
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        stderr = os.fsdecode(result.stderr).strip()
        raise RuntimeError(f"git log failed: {stderr}")
    output = result.stdout.strip()
    return int(output) if output else 0


def _own_uncommitted_paths(
    repo_path: str,
    dirty_paths: list[str],
    edit_times: dict[str, float],
) -> list[str]:
    """Return the dirty paths the task's edit ledger cannot prove are someone else's.

    A path is the task's own uncommitted work when the session never recorded an edit
    of it under this task, or when its newest recorded edit postdates the last commit
    touching it. ``%ct`` is second-resolution, so the stamp is floored before comparing.
    """
    own: list[str] = []
    for path in dirty_paths:
        edited_at = edit_times.get(path)
        if edited_at is None or int(edited_at) > _last_commit_epoch(repo_path, path):
            own.append(path)
    return own


def register_release_task_paths(
    registry: InternalToolRegistry,
    ctx: RegistryContext,
) -> None:
    """Register task path ownership inspection and owner-controlled release."""

    def inspect_task_path_ownership() -> dict[str, Any]:
        """Inspect dirty and staged path attribution in the caller's checkout."""
        session_ref = get_current_session_id()
        if not session_ref:
            return task_error(
                "No session context available. Ensure session_id is set.",
                TaskToolErrorCode.SESSION_REQUIRED,
            )
        try:
            session_id = ctx.resolve_session_id(session_ref)
            project_id = ctx.resolve_project_from_session(session_ref)
            checkout_root = _lifecycle_checkout_root(
                ctx,
                session_id=session_id,
                project_id=project_id,
                overlay_path=_claimed_session_worktree_path(
                    ctx,
                    session_id=session_id,
                    project_id=project_id,
                ),
            )
        except ValueError as exc:
            return task_error(str(exc), TaskToolErrorCode.TASK_INVALID_STATUS)
        if checkout_root is None:
            return task_error(
                "Cannot inspect path ownership because the project has no repository path",
                TaskToolErrorCode.TASK_INVALID_STATUS,
            )
        try:
            ownership = inspect_checkout_path_ownership(
                ctx.task_manager.db,
                project_id=project_id,
                checkout_root=checkout_root,
            )
        except DirtyEditOwnershipInspectionError as exc:
            return task_error(
                f"Cannot inspect path ownership: {exc}",
                TaskToolErrorCode.TASK_INVALID_STATUS,
            )

        paths = [
            {
                "path": item.path,
                "dirty": item.dirty,
                "staged": item.staged,
                "owners": [
                    {"task": owner.task_ref, "session": owner.session_ref} for owner in item.owners
                ],
                "unowned": not item.owners,
            }
            for item in ownership
        ]
        return {
            "success": True,
            "project_id": project_id,
            "checkout_root": checkout_root,
            "paths": paths,
            "unowned_paths": [item["path"] for item in paths if item["unowned"]],
        }

    registry.register(
        name="inspect_task_path_ownership",
        description=(
            "Read-only inspection of every dirty or staged path in the caller's checkout, "
            "including active owner session/task references and explicit unowned entries."
        ),
        input_schema={"type": "object", "properties": {}},
        func=inspect_task_path_ownership,
    )

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
        try:
            session_worktree_path = _claimed_session_worktree_path(
                ctx,
                session_id=session_id,
                project_id=task.project_id,
            )
            repo_path = _lifecycle_checkout_root(
                ctx,
                session_id=session_id,
                project_id=task.project_id,
                overlay_path=session_worktree_path or artifacts.worktree_path,
            )
        except ValueError as exc:
            return task_error(str(exc), TaskToolErrorCode.TASK_INVALID_STATUS)
        if repo_path is None:
            return task_error(
                "Cannot verify task paths because the project has no repository path",
                TaskToolErrorCode.TASK_INVALID_STATUS,
            )
        try:
            dirty_paths = _dirty_repo_paths(repo_path, normalized_paths)
            # Dirt is this task's own uncommitted work unless the session's edit ledger
            # shows its newest edit of the path predates the last commit touching it;
            # only then is releasing the attribution safe (#20818 and its reverse:
            # another session also holding attribution proves nothing about whose
            # dirt it is).
            own_dirty_paths = _own_uncommitted_paths(
                repo_path,
                dirty_paths,
                task_edited_file_times(
                    ctx.session_var_manager.get_variables(session_id), resolved_task_id
                ),
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return task_error(
                f"Cannot verify task paths: {exc}",
                TaskToolErrorCode.TASK_INVALID_STATUS,
            )
        if own_dirty_paths:
            return task_error(
                "Cannot release paths whose uncommitted content may be this task's own "
                "work (no recorded edit, or the newest recorded edit is newer than the "
                "last commit touching the path); commit or revert it first "
                "(git stash is blocked for interactive sessions)",
                TaskToolErrorCode.TASK_INVALID_STATUS,
                dirty_paths=own_dirty_paths,
            )
        foreign_owned: dict[str, Any] = {}
        if dirty_paths:
            # Report which other active sessions' open tasks hold the released dirt; the
            # report is informational, so an inspection failure omits it.
            try:
                foreign_owned = foreign_owned_dirty_paths(
                    ctx.task_manager.db,
                    session_id=session_id,
                    project_id=task.project_id,
                    checkout_root=repo_path,
                    paths=set(dirty_paths),
                )
            except DirtyEditOwnershipInspectionError:
                logger.warning(
                    "Dirty-path ownership inspection failed during release_task_paths; "
                    "omitting the foreign_dirty_paths report",
                    extra={"task_id": resolved_task_id, "session_id": session_id},
                    exc_info=True,
                )

        released, remaining = ctx.session_var_manager.release_task_edited_files(
            session_id,
            resolved_task_id,
            normalized_paths,
            checkout_root=repo_path,
        )
        result: dict[str, Any] = {
            "success": True,
            "task_id": resolved_task_id,
            "released_paths": released,
            "remaining_paths": remaining,
        }
        if foreign_owned:
            result["foreign_dirty_paths"] = {
                path: [{"task": owner.task_ref, "session": owner.session_ref} for owner in owners]
                for path, owners in sorted(foreign_owned.items())
            }
        return result

    registry.register(
        name="release_task_paths",
        description=(
            "Release committed or abandoned paths from the current session's claimed task "
            "attribution. A path with uncommitted content is releasable only when this "
            "session's newest recorded edit of it under the task predates the last commit "
            "touching it, so the dirt is someone else's (other active sessions' open tasks "
            "holding it are reported); a path with no recorded edit, or edited since that "
            "commit, must be committed or reverted before releasing."
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
