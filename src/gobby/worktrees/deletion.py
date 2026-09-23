"""Worktree deletion with asynchronous Git and guarded database cleanup."""

from __future__ import annotations

import contextvars
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.agents.cargo_target import cleanup_checkout_cargo_target_dir
from gobby.utils.git import run_thread_to_completion
from gobby.worktrees.events import WorktreeEvent, emit_worktree_event
from gobby.worktrees.executor import DestructiveBoundary

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager, Worktree
    from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger(__name__)


class DeletionSurface(Enum):
    """Behavioral contract of a deletion transport."""

    MCP = "mcp"
    HTTP = "http"
    MAINTENANCE = "maintenance"


@dataclass(frozen=True)
class WorktreeDeletionRequest:
    """Inputs for one complete deletion transaction."""

    worktree_id: str
    surface: DeletionSurface
    force: bool = False
    force_delete_branch: bool = False
    merged_into: str | None = None


@dataclass(frozen=True)
class WorktreeDeletionResult:
    """Transport-neutral result of one deletion transaction."""

    success: bool
    found: bool = True
    git_deleted: bool = True
    error: str | None = None
    error_code: str | None = None
    uncommitted_changes: bool = False
    artifact_refs_cleared: int = 0
    event: WorktreeEvent | None = None
    abandoned: bool = False


type GitManagerResolver = Callable[[Worktree], WorktreeGitManager | None]


async def delete_worktree_transaction(
    boundary: DestructiveBoundary,
    *,
    request: WorktreeDeletionRequest,
    worktree_storage: LocalWorktreeManager,
    resolve_git_manager: GitManagerResolver,
    task_manager: LocalTaskManager | None,
) -> WorktreeDeletionResult:
    """Perform lookup, nonblocking Git mutation, storage cleanup, and event emission."""
    context = contextvars.copy_context()
    if request.surface is DeletionSurface.MAINTENANCE:
        guard = worktree_storage.lock_for_cleanup(request.worktree_id)
        entered = False

        def enter_guard() -> Worktree | None:
            nonlocal entered
            worktree = guard.__enter__()
            entered = True
            return worktree

        try:
            worktree = await run_thread_to_completion(context.run, enter_guard)
            if worktree is None:
                return WorktreeDeletionResult(
                    success=False, git_deleted=False, error="Worktree is no longer eligible"
                )
            return await _delete_worktree(
                boundary,
                request,
                worktree,
                worktree_storage,
                resolve_git_manager,
                task_manager,
                context,
            )
        finally:
            if entered:
                await run_thread_to_completion(context.run, guard.__exit__, *sys.exc_info())
    worktree = await run_thread_to_completion(
        context.run, worktree_storage.get, request.worktree_id
    )
    if worktree is None:
        return WorktreeDeletionResult(success=True, found=False)
    return await _delete_worktree(
        boundary, request, worktree, worktree_storage, resolve_git_manager, task_manager, context
    )


async def _delete_worktree(
    boundary: DestructiveBoundary,
    request: WorktreeDeletionRequest,
    worktree: Worktree,
    worktree_storage: LocalWorktreeManager,
    resolve_git_manager: GitManagerResolver,
    task_manager: LocalTaskManager | None,
    context: contextvars.Context,
) -> WorktreeDeletionResult:
    git_manager = await run_thread_to_completion(context.run, resolve_git_manager, worktree)
    if request.merged_into is not None and git_manager is None:
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            error="Cannot verify merged_into without a resolved git manager",
            error_code="merged_into_requires_git_manager",
        )
    worktree_exists = Path(worktree.worktree_path).exists()
    if worktree_exists and git_manager is None:
        return WorktreeDeletionResult(
            success=False,
            error=(
                "Cannot delete an on-disk worktree without a resolved git manager; "
                "the worktree record was preserved"
            ),
        )
    if request.surface is not DeletionSurface.HTTP:
        precheck = await _mcp_precheck(request, worktree, git_manager, worktree_exists)
        if precheck is not None:
            return precheck

    if not boundary.begin_mutation():
        return WorktreeDeletionResult(success=False, abandoned=True)

    git_failure = await _delete_git_worktree(request, worktree, git_manager)
    if git_failure is not None:
        return git_failure

    cargo_cleanup_error = await run_thread_to_completion(
        context.run,
        cleanup_checkout_cargo_target_dir,
        Path(worktree.worktree_path),
        worktree.project_id,
    )
    if cargo_cleanup_error is not None:
        return WorktreeDeletionResult(
            success=False,
            git_deleted=True,
            error=(
                "Git worktree files were deleted, but Cargo target cleanup failed: "
                f"{cargo_cleanup_error}"
            ),
            error_code="cargo_target_cleanup_failed",
        )

    return await run_thread_to_completion(
        context.run, _finish_storage_deletion, request, worktree, worktree_storage, task_manager
    )


def _finish_storage_deletion(
    request: WorktreeDeletionRequest,
    worktree: Worktree,
    worktree_storage: LocalWorktreeManager,
    task_manager: LocalTaskManager | None,
) -> WorktreeDeletionResult:
    if not worktree_storage.delete(request.worktree_id):
        return WorktreeDeletionResult(
            success=False,
            error="Failed to delete worktree record",
        )

    artifact_refs_cleared = _clear_artifact_references(task_manager, request.worktree_id)
    event = emit_worktree_event(
        "worktree_deleted",
        worktree_id=request.worktree_id,
        project_id=worktree.project_id,
        branch_name=worktree.branch_name,
        worktree_path=worktree.worktree_path,
        artifact_refs_cleared=artifact_refs_cleared,
    )
    return WorktreeDeletionResult(
        success=True,
        artifact_refs_cleared=artifact_refs_cleared,
        event=event,
    )


async def _mcp_precheck(
    request: WorktreeDeletionRequest,
    worktree: Worktree,
    git_manager: WorktreeGitManager | None,
    worktree_exists: bool,
) -> WorktreeDeletionResult | None:
    if request.surface is DeletionSurface.MAINTENANCE:
        if (
            git_manager is None
            or not worktree_exists
            or request.force
            or request.force_delete_branch
        ):
            return WorktreeDeletionResult(
                success=False, git_deleted=False, error="Cannot safely verify expired worktree"
            )
        status = await git_manager.get_worktree_status(worktree.worktree_path)
        if status is None or status.branch != worktree.branch_name or status.branch is None:
            return WorktreeDeletionResult(
                success=False, git_deleted=False, error="Cannot verify the expired worktree branch"
            )
    if git_manager is None or not worktree_exists:
        return None

    status = await git_manager.get_worktree_status(worktree.worktree_path)
    if status is None:
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            error="Cannot verify worktree cleanliness; the worktree record was preserved",
            error_code="worktree_status_unavailable",
        )
    if status.has_uncommitted_changes and not request.force:
        return WorktreeDeletionResult(
            success=False,
            error="Worktree has uncommitted changes. Use force=True to delete anyway.",
            uncommitted_changes=True,
        )
    return None


async def _delete_git_worktree(
    request: WorktreeDeletionRequest,
    worktree: Worktree,
    git_manager: WorktreeGitManager | None,
) -> WorktreeDeletionResult | None:
    if git_manager is None:
        logger.info(
            "Worktree path %s doesn't exist, cleaning up DB record only",
            worktree.worktree_path,
        )
        return None

    git_already_deleted, retry_error = await probe_missing_worktree_git_state(
        git_manager,
        worktree_path=worktree.worktree_path,
        branch_name=worktree.branch_name,
    )
    if retry_error is not None:
        return WorktreeDeletionResult(success=False, git_deleted=False, error=retry_error)
    if git_already_deleted:
        return None

    try:
        result = await git_manager.delete_worktree(
            worktree.worktree_path,
            force=True if request.surface is DeletionSurface.HTTP else request.force,
            delete_branch=True,
            force_delete_branch=request.force_delete_branch,
            branch_name=worktree.branch_name,
            base_branch=worktree.base_branch,
            merged_into=request.merged_into,
        )
    except Exception as exc:
        if request.surface is DeletionSurface.MCP:
            raise
        logger.warning("Git worktree deletion raised an exception", exc_info=True)
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            error=str(exc),
        )

    if result.success:
        return None
    if request.merged_into is not None or request.surface is DeletionSurface.MAINTENANCE:
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            error=result.message,
            error_code=result.error,
        )
    if request.surface is DeletionSurface.HTTP:
        logger.warning("Git worktree deletion failed: %s", result.message)
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            error=result.message,
        )
    if Path(worktree.worktree_path).exists():
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            error=result.error or "Failed to delete git worktree",
        )

    prune = getattr(git_manager, "prune_worktrees", None)
    if not callable(prune):
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            error=result.error or "Failed to prune missing git worktree",
        )
    prune_result = await prune()
    if prune_result.success:
        return None
    return WorktreeDeletionResult(
        success=False,
        git_deleted=False,
        error=prune_result.error or result.error or "Failed to prune missing git worktree",
    )


async def probe_missing_worktree_git_state(
    git_manager: WorktreeGitManager,
    *,
    worktree_path: str | Path,
    branch_name: str | None,
) -> tuple[bool, str | None]:
    """Confirm a missing checkout and branch need no repeated Git deletion.

    Returns ``(True, None)`` only after pruning a missing worktree registration.
    A present path or branch returns ``(False, None)`` so normal deletion still runs.
    """
    if Path(worktree_path).exists():
        return False, None
    if not branch_name:
        return False, "Cannot verify missing worktree without its source branch"
    try:
        branch_result = await git_manager.run_git_command(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            timeout=5,
        )
    except Exception as exc:
        return False, f"Failed to verify missing worktree branch: {exc}"
    if branch_result.returncode == 0:
        return False, None
    if branch_result.returncode != 1:
        return False, branch_result.stderr.strip() or "Failed to verify missing worktree branch"
    prune_result = await git_manager.prune_worktrees()
    if not prune_result.success:
        return False, prune_result.error or "Failed to prune missing git worktree"
    return True, None


def _clear_artifact_references(
    task_manager: LocalTaskManager | None,
    worktree_id: str,
) -> int:
    if task_manager is None:
        return 0
    try:
        return task_manager.artifacts.clear_worktree_references(worktree_id)
    except Exception:
        logger.warning(
            "Failed to clear task artifact worktree references after deletion",
            extra={"operation": "delete_worktree", "worktree_id": worktree_id},
            exc_info=True,
        )
        return 0
