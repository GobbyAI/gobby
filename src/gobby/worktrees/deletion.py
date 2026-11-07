"""Worktree deletion with asynchronous Git and guarded database cleanup."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.agents.cargo_target import cleanup_checkout_cargo_target_dir
from gobby.agents.isolation_git_hygiene import blocking_status_lines
from gobby.utils.git import run_thread_to_completion
from gobby.worktrees.events import WorktreeEvent, emit_worktree_event
from gobby.worktrees.executor import DestructiveBoundary
from gobby.worktrees.git import worktree_registration_is_absent
from gobby.worktrees.occupancy import refuse_occupied_worktree

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
    TEARDOWN = "teardown"


# Daemon-owned deletions lock the row and prove the tree's branch before Git runs.
_DAEMON_SURFACES = frozenset({DeletionSurface.MAINTENANCE, DeletionSurface.TEARDOWN})


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
    message: str | None = None
    uncommitted_changes: bool = False
    artifact_refs_cleared: int = 0
    event: WorktreeEvent | None = None
    abandoned: bool = False


type GitManagerResolver = Callable[[Worktree], WorktreeGitManager | None]


_IN_FLIGHT: set[str] = set()
_IN_FLIGHT_LOCK = threading.Lock()


def _claim_deletion(worktree_id: str) -> bool:
    with _IN_FLIGHT_LOCK:
        if worktree_id in _IN_FLIGHT:
            return False
        _IN_FLIGHT.add(worktree_id)
        return True


def _release_deletion(worktree_id: str) -> None:
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.discard(worktree_id)


async def delete_worktree_transaction(
    boundary: DestructiveBoundary,
    *,
    request: WorktreeDeletionRequest,
    worktree_storage: LocalWorktreeManager,
    resolve_git_manager: GitManagerResolver,
    task_manager: LocalTaskManager | None,
) -> WorktreeDeletionResult:
    """Run one deletion per worktree at a time, whichever surface asks.

    A concurrent caller is refused before any lookup or Git call, so a second removal
    never races the first one's tree (#25840). Every surface enters through here.
    """
    if not _claim_deletion(request.worktree_id):
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            abandoned=True,
            error="Worktree deletion is already in progress; the next pass retries",
            error_code="deletion_in_progress",
        )
    try:
        return await _run_delete_transaction(
            boundary,
            request=request,
            worktree_storage=worktree_storage,
            resolve_git_manager=resolve_git_manager,
            task_manager=task_manager,
        )
    finally:
        _release_deletion(request.worktree_id)


async def _run_delete_transaction(
    boundary: DestructiveBoundary,
    *,
    request: WorktreeDeletionRequest,
    worktree_storage: LocalWorktreeManager,
    resolve_git_manager: GitManagerResolver,
    task_manager: LocalTaskManager | None,
) -> WorktreeDeletionResult:
    """Perform lookup, nonblocking Git mutation, storage cleanup, and event emission."""
    context = contextvars.copy_context()
    if request.surface in _DAEMON_SURFACES:
        # A closed team's tree is due at once; only maintenance waits for the expiry.
        guard = worktree_storage.lock_for_cleanup(
            request.worktree_id, expired_only=request.surface is DeletionSurface.MAINTENANCE
        )
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
                    success=False,
                    git_deleted=False,
                    error="Worktree is no longer eligible",
                    error_code="worktree_not_eligible",
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
    occupied = await run_thread_to_completion(
        context.run,
        refuse_occupied_worktree,
        worktree_storage.db,
        worktree.worktree_path,
        worktree_id=worktree.id,
    )
    if occupied is not None:
        return WorktreeDeletionResult(
            success=False, git_deleted=False, error=occupied, error_code="worktree_in_use"
        )
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
    force = request.force or request.surface is DeletionSurface.HTTP
    generated_untracked: list[str] = []
    if request.surface in _DAEMON_SURFACES:
        verdict = await _daemon_precheck(request, worktree, git_manager, worktree_exists)
        if isinstance(verdict, WorktreeDeletionResult):
            return verdict
        generated_untracked = verdict
    elif request.surface is DeletionSurface.MCP:
        precheck = await _mcp_precheck(request, worktree, git_manager, worktree_exists)
        if precheck is not None:
            return precheck

    if not boundary.begin_mutation():
        return WorktreeDeletionResult(success=False, abandoned=True)

    if generated_untracked:
        cleanup_error = await run_thread_to_completion(
            context.run,
            _clear_generated_untracked,
            Path(worktree.worktree_path),
            generated_untracked,
        )
        if cleanup_error is not None:
            return WorktreeDeletionResult(
                success=False,
                git_deleted=False,
                error=cleanup_error,
                error_code="worktree_generated_cleanup_failed",
            )
    logger.info(
        "Deleting worktree %s: surface=%s force=%s generated_untracked=%d",
        worktree.worktree_path,
        request.surface.value,
        force,
        len(generated_untracked),
    )
    git_failure = await _delete_git_worktree(request, worktree, git_manager, force=force)
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


def _remove_empty_shell(path: Path) -> bool:
    """Remove a non-worktree directory that holds no file; True when it was removed.

    A seat relaunched into a deleted tree recreates empty scratch directories
    (``.gobby/tmp``, ``.nextest``) at its path. They hold nothing, so the tree is
    as gone as a missing one and its record must stay deletable (#25165).
    """
    if any(entry.is_file() or entry.is_symlink() for entry in path.rglob("*")):
        return False
    shutil.rmtree(path)
    return True


def _finish_storage_deletion(
    request: WorktreeDeletionRequest,
    worktree: Worktree,
    worktree_storage: LocalWorktreeManager,
    task_manager: LocalTaskManager | None,
) -> WorktreeDeletionResult:
    failure: str | None = None
    try:
        if not worktree_storage.delete(request.worktree_id):
            failure = "Failed to delete worktree record"
    except Exception as exc:
        # Git and the filesystem already agree the tree is gone; the record survives so the
        # next pass sees a missing tree and retries only this record delete.
        logger.warning(
            "Worktree %s was removed but its record delete failed",
            request.worktree_id,
            exc_info=True,
        )
        failure = f"Worktree files were removed, but the record delete failed: {exc}"
    if failure is not None:
        return WorktreeDeletionResult(
            success=False,
            git_deleted=True,
            error=failure,
            error_code="worktree_record_delete_failed",
            abandoned=True,
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


async def _daemon_precheck(
    request: WorktreeDeletionRequest,
    worktree: Worktree,
    git_manager: WorktreeGitManager | None,
    worktree_exists: bool,
) -> WorktreeDeletionResult | list[str]:
    """Refuse what a daemon deletion cannot prove; else the generated untracked paths to clear."""
    if git_manager is None or request.force or request.force_delete_branch:
        return WorktreeDeletionResult(
            success=False, git_deleted=False, error="Cannot safely verify the worktree"
        )
    if not worktree_exists:
        # Git already removed the tree (a teardown crashed after it). The missing-tree
        # probe proves the registration and branch are gone before the record is deleted.
        return []
    # Git deletes the branch the tree holds, so a tree switched off its record keeps both.
    status = await git_manager.get_worktree_status(worktree.worktree_path)
    if status is None or status.branch != worktree.branch_name or status.branch is None:
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            error=f"Cannot verify the worktree is on its recorded branch {worktree.branch_name}",
            error_code="worktree_identity_mismatch" if status else "worktree_status_unavailable",
        )
    porcelain = await git_manager.run_git_command(
        ["status", "--porcelain"], cwd=worktree.worktree_path, timeout=10
    )
    if porcelain.returncode != 0:
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            error="Cannot verify worktree cleanliness; the worktree record was preserved",
            error_code="worktree_status_unavailable",
        )
    lines = [line for line in porcelain.stdout.splitlines() if line.strip()]
    # A tracked line is real work even under a generated path: isolation keeps generated
    # files out of tracked status, so only untracked lines can be spawn-generated.
    tracked = [line for line in lines if not line.startswith("?? ")]
    untracked = [line for line in lines if line.startswith("?? ")]
    path = Path(worktree.worktree_path)
    if tracked or blocking_status_lines("\n".join(untracked), path, git_manager.repo_path):
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            error="Worktree has uncommitted changes",
            uncommitted_changes=True,
        )
    # Every untracked path is Gobby-generated; the caller clears them so a non-forced
    # removal never needs --force.
    return [line[3:].strip().strip('"') for line in untracked]


async def _mcp_precheck(
    request: WorktreeDeletionRequest,
    worktree: Worktree,
    git_manager: WorktreeGitManager | None,
    worktree_exists: bool,
) -> WorktreeDeletionResult | None:
    if git_manager is None or not worktree_exists:
        return None

    status = await git_manager.get_worktree_status(worktree.worktree_path)
    if status is None and await asyncio.to_thread(
        _remove_empty_shell, Path(worktree.worktree_path)
    ):
        return None  # the missing-tree probe now proves registration and branch are gone
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


def _clear_generated_untracked(worktree: Path, relpaths: list[str]) -> str | None:
    """Delete spawn-generated untracked paths so a non-forced removal can proceed.

    Only paths inside the tree are touched; a symlink is unlinked, never followed.
    """
    for relpath in relpaths:
        rel = Path(relpath)
        if rel.is_absolute() or not rel.parts or ".." in rel.parts:
            return f"Refusing to clear a path outside the worktree: {relpath}"
        target = worktree / rel
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
        except OSError as exc:
            return f"Cannot clear generated path {relpath}: {exc}"
    return None


async def _verify_removed(git_manager: WorktreeGitManager, worktree_path: str) -> str | None:
    """Return why a reported removal is not real, or None when the filesystem and Git agree."""
    path = Path(worktree_path)
    if path.exists():
        return f"Git reported the worktree removed, but {worktree_path} still exists"
    absent, error = await worktree_registration_is_absent(git_manager, path)
    if error is not None:
        return error
    if not absent:
        return f"Git still registers {worktree_path} as a worktree"
    return None


async def _delete_git_worktree(
    request: WorktreeDeletionRequest,
    worktree: Worktree,
    git_manager: WorktreeGitManager | None,
    *,
    force: bool,
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
        merged_into=request.merged_into if not request.force_delete_branch else None,
    )
    if retry_error is not None:
        return WorktreeDeletionResult(success=False, git_deleted=False, error=retry_error)
    if git_already_deleted:
        return None

    try:
        result = await git_manager.delete_worktree(
            worktree.worktree_path,
            force=force,
            delete_branch=True,
            force_delete_branch=request.force_delete_branch,
            # Reused seats may have switched branches since registration. Git
            # must prove and delete the branch currently attached to this tree.
            branch_name=None if Path(worktree.worktree_path).exists() else worktree.branch_name,
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
        unverified = await _verify_removed(git_manager, worktree.worktree_path)
        if unverified is None:
            return None
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            error=unverified,
            error_code="worktree_removal_unverified",
        )
    if request.merged_into is not None or request.surface in _DAEMON_SURFACES:
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            error=result.message,
            # Some Git failures carry only stderr text, which is not a code.
            error_code=result.error if result.error and result.error.isidentifier() else None,
            message=result.message,
        )
    if request.surface is DeletionSurface.HTTP:
        logger.warning("Git worktree deletion failed: %s", result.message)
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            error=result.message,
            message=result.message,
        )
    if Path(worktree.worktree_path).exists():
        return WorktreeDeletionResult(
            success=False,
            git_deleted=False,
            error=result.error or "Failed to delete git worktree",
            message=result.message,
        )

    git_already_deleted, probe_error = await probe_missing_worktree_git_state(
        git_manager,
        worktree_path=worktree.worktree_path,
        branch_name=worktree.branch_name,
    )
    if git_already_deleted:
        return None
    return WorktreeDeletionResult(
        success=False,
        git_deleted=False,
        error=probe_error or result.error or "Git worktree registration was preserved",
    )


async def probe_missing_worktree_git_state(
    git_manager: WorktreeGitManager,
    *,
    worktree_path: str | Path,
    branch_name: str | None,
    merged_into: str | None = None,
) -> tuple[bool, str | None]:
    """Confirm a missing checkout and branch need no repeated Git deletion.

    Returns ``(True, None)`` only after confirming the Git registration is gone.
    A detached registration needs an explicit landing target and Git's recorded HEAD.
    A present path or branch returns ``(False, None)`` so normal deletion still runs.
    """
    if Path(worktree_path).exists():
        return False, None
    if not branch_name:
        if merged_into is not None:
            return await _remove_missing_detached_worktree(
                git_manager, Path(worktree_path), merged_into
            )
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
    return await worktree_registration_is_absent(git_manager, Path(worktree_path))


async def _remove_missing_detached_worktree(
    git_manager: WorktreeGitManager, worktree_path: Path, merged_into: str
) -> tuple[bool, str | None]:
    """Remove only a missing detached registration whose recorded HEAD is landed."""
    target_ref = merged_into if merged_into.startswith("refs/") else f"refs/heads/{merged_into}"
    if merged_into.startswith("origin/") or not target_ref.startswith("refs/heads/"):
        return False, "Missing detached worktree requires a local landing branch"
    try:
        valid = await git_manager.run_git_command(["check-ref-format", target_ref], timeout=5)
        symbolic = await git_manager.run_git_command(
            ["symbolic-ref", "--quiet", target_ref], timeout=5
        )
        if valid.returncode != 0 or symbolic.returncode != 1:
            return False, "Missing detached worktree requires a direct local landing branch"
        records = [
            item
            for item in await git_manager.list_worktrees()
            if Path(item.path).resolve() == worktree_path.resolve()
        ]
        if len(records) != 1:
            return False, "Cannot verify the missing detached worktree's Git registration"
        record = records[0]
        if (
            not record.is_detached
            or record.branch is not None
            or record.is_bare
            or record.locked
            or not record.commit
        ):
            return False, "Cannot verify an unlocked detached worktree HEAD"
        merged = await git_manager.run_git_command(
            ["merge-base", "--is-ancestor", record.commit, target_ref], timeout=10
        )
        if merged.returncode != 0:
            return False, "Missing detached worktree HEAD is not proven landed"
        if worktree_path.exists() or worktree_path.is_symlink():
            return False, "Missing detached worktree path reappeared; registration preserved"
        # Target just this registration. A global prune could retire unrelated unlanded roots.
        removed = await git_manager.run_git_command(
            ["worktree", "remove", str(worktree_path)], timeout=30
        )
        if removed.returncode != 0:
            return False, removed.stderr.strip() or "Failed to remove missing detached worktree"
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return False, f"Failed to verify missing detached worktree: {exc}"
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
