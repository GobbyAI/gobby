"""Worktree and clone isolation maintenance."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.agents.cargo_target import cleanup_checkout_cargo_target_dir
from gobby.agents.team_teardown import (
    SEAT_NOT_ENDED,
    TeamRunTerminator,
    TeamSeatNotEndedError,
)
from gobby.clones.git import CloneGitManager, GitOperationResult
from gobby.hub.remote_workspaces import expire_remote_workspaces
from gobby.mcp_proxy.tools import agent_cancellation
from gobby.runner_maintenance.isolation_reconciliation import reconcile_isolation_registry
from gobby.runner_maintenance_helpers import _run_db
from gobby.storage.agents import AgentRun, LocalAgentRunManager, task_closed_terminal_reason
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import require_root
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from gobby.utils.git import run_thread_to_completion
from gobby.worktrees.deletion import (
    DeletionSurface,
    WorktreeDeletionRequest,
    WorktreeDeletionResult,
    delete_worktree_transaction,
)
from gobby.worktrees.executor import (
    DestructiveBoundary,
    WorktreeDeleteExecutor,
    run_worktree_delete,
)
from gobby.worktrees.git import WorktreeGitManager
from gobby.worktrees.recovery import recover_deleting_worktrees

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger("gobby.runner_maintenance")
_ISOLATION_CLEANUP_SCAN_LIMIT = 1000


async def cleanup_expired_isolation_loop(
    db: HubDatabase,
    is_shutdown_requested: Callable[[], bool],
    interval_hours: int = 1,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    worktree_delete_executor: WorktreeDeleteExecutor | None = None,
) -> None:
    """Reap expired worktrees and clones whose cleanup_after window has passed.

    Expiry selects candidates. The worker rechecks ownership and local Git
    evidence before deleting any directory, branch or database record.
    """
    worktree_storage = LocalWorktreeManager(db)
    clone_storage = LocalCloneManager(db)
    interval_seconds = interval_hours * 3600

    while not is_shutdown_requested():
        try:
            await asyncio.sleep(interval_seconds)

            # Reap expired worktrees
            expired_worktrees = await _run_db(run_db, worktree_storage.find_expired)
            for wt in expired_worktrees:
                try:
                    request = WorktreeDeletionRequest(wt.id, DeletionSurface.MAINTENANCE)
                    result = await run_worktree_delete(
                        worktree_delete_executor,
                        partial(
                            delete_worktree_transaction,
                            request=request,
                            worktree_storage=worktree_storage,
                            resolve_git_manager=partial(_worktree_git, db),
                            task_manager=LocalTaskManager(db),
                        ),
                    )
                    if not result.success or not result.found:
                        logger.debug("Skipped expired worktree %s: %s", wt.id, result.error)
                        continue
                    logger.info(
                        "Expired worktree cleanup: deleted %s (branch=%s, path=%s)",
                        wt.id,
                        wt.branch_name,
                        wt.worktree_path,
                    )
                except Exception:
                    logger.exception(
                        "Failed to clean up expired worktree %s",
                        wt.id,
                    )

            # Reap expired clones
            expired_clones = await _run_db(run_db, clone_storage.find_expired)
            for clone in expired_clones:
                try:

                    async def delete_clone(
                        boundary: DestructiveBoundary, clone_id: str = clone.id
                    ) -> GitOperationResult:
                        return await _delete_expired_clone(db, clone_storage, clone_id, boundary)

                    clone_result = await run_worktree_delete(
                        worktree_delete_executor,
                        delete_clone,
                    )
                    if not clone_result.success:
                        logger.debug("Skipped expired clone %s: %s", clone.id, clone_result.message)
                        continue
                    logger.info(
                        "Expired clone cleanup: deleted %s (branch=%s, path=%s)",
                        clone.id,
                        clone.branch_name,
                        clone.clone_path,
                    )
                except Exception:
                    logger.exception(
                        "Failed to clean up expired clone %s",
                        clone.id,
                    )

            await expire_remote_workspaces(db, run_db=run_db)

            await _cleanup_missing_isolation_records_async(
                worktree_storage,
                clone_storage,
                run_db=run_db,
                worktree_delete_executor=worktree_delete_executor,
            )
            await reconcile_isolation_registry(db, run_db=run_db)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in expired isolation cleanup loop: %s", e)


def team_run_terminator(runner: GobbyRunner) -> TeamRunTerminator:
    """End a team seat's run through the daemon termination lifecycle, closing its tab."""

    async def terminate(run: AgentRun, closed_reason: str | None) -> bool:
        db = runner.database
        result = await agent_cancellation.terminate_agent_run(
            run=run,
            runner=runner.agent_runner,
            agent_run_manager=LocalAgentRunManager(db),
            db=db,
            lifecycle_monitor=runner.agent_lifecycle_monitor,
            completion_registry=runner.completion_registry,
            task_manager=runner.task_manager,
            session_manager=SessionManager(db),
            effective_status="success" if closed_reason == "done" else "cancelled",
            terminal_reason=task_closed_terminal_reason(closed_reason),
            debug=False,
        )
        if result.get("success"):
            return True
        if result.get("error_code") == agent_cancellation.TERMINATE_DAEMON_SHUTDOWN:
            return False  # admission is closed for shutdown; the next pass retries
        raise TeamSeatNotEndedError(
            str(result.get("error_code") or SEAT_NOT_ENDED),
            str(result.get("error") or f"run {run.id} could not be ended"),
        )

    return terminate


def deleting_worktree_recovery(
    db: HubDatabase,
    executor: WorktreeDeleteExecutor | None,
    should_stop: Callable[[], bool],
) -> Callable[[], Awaitable[list[WorktreeDeletionResult]]]:
    """Finish worktree records a shutdown left deleting, through the same git resolver."""

    async def recover() -> list[WorktreeDeletionResult]:
        return await recover_deleting_worktrees(
            worktree_storage=LocalWorktreeManager(db),
            resolve_git_manager=partial(_worktree_git, db),
            task_manager=LocalTaskManager(db),
            executor=executor,
            should_stop=should_stop,
        )

    return recover


def _worktree_git(db: HubDatabase, worktree: Worktree) -> WorktreeGitManager:
    return WorktreeGitManager(require_root(db, worktree.project_id, worktree.machine_id))


async def _delete_expired_clone(
    db: HubDatabase,
    storage: LocalCloneManager,
    clone_id: str,
    boundary: DestructiveBoundary,
) -> GitOperationResult:
    """Verify the clone's exact HEAD is retained in the parent before removing it."""
    with storage.lock_for_cleanup(clone_id) as clone:
        if clone is None:
            return GitOperationResult(False, "Clone is no longer eligible")
        root = require_root(db, clone.project_id, clone.machine_id)
        manager = CloneGitManager(root)
        path = manager.resolve_managed_clone_path(clone.clone_path)
        if path is None:
            return GitOperationResult(False, "Clone path is outside managed storage")
        status = await manager.get_clone_status(path)
        if (
            status is None
            or status.branch is None
            or status.branch != clone.branch_name
            or status.has_uncommitted_changes
            or status.has_staged_changes
            or status.has_untracked_files
        ):
            return GitOperationResult(False, "Clone branch or clean state cannot be verified")
        target = clone.base_branch
        if target.startswith("origin/") or (
            target.startswith("refs/") and not target.startswith("refs/heads/")
        ):
            return GitOperationResult(False, "Clone base must be a local branch")
        target_ref = target if target.startswith("refs/heads/") else f"refs/heads/{target}"
        if (
            await manager.run_git_command(["check-ref-format", target_ref], timeout=5)
        ).returncode != 0:
            return GitOperationResult(False, "Clone base is not a valid local branch")
        if (
            await manager.run_git_command(["symbolic-ref", "--quiet", target_ref], timeout=5)
        ).returncode != 1:
            return GitOperationResult(False, "Clone base must be a direct local branch")
        head = await manager.run_git_command(["rev-parse", "--verify", "HEAD"], cwd=path, timeout=5)
        if head.returncode != 0 or not head.stdout.strip():
            return GitOperationResult(False, "Clone HEAD cannot be verified")
        proof = await manager.run_git_command(
            ["merge-base", "--is-ancestor", head.stdout.strip(), target_ref], timeout=10
        )
        if proof.returncode != 0:
            return GitOperationResult(False, "Clone HEAD is not merged into its local base")
        if not boundary.begin_mutation():
            return GitOperationResult(False, "Clone cleanup cancelled before mutation")
        result = await manager.delete_clone(path, force=False)
        if result.success:
            cargo_error = await asyncio.to_thread(
                cleanup_checkout_cargo_target_dir,
                Path(clone.clone_path),
                clone.project_id,
            )
            if cargo_error is not None:
                return GitOperationResult(
                    False,
                    f"Clone files were deleted, but Cargo target cleanup failed: {cargo_error}",
                    error="cargo_target_cleanup_failed",
                )
            if not storage.delete(clone_id):
                return GitOperationResult(False, "Failed to delete clone record")
        return result


async def _cleanup_missing_isolation_records(
    worktree_storage: LocalWorktreeManager,
    clone_storage: LocalCloneManager,
    *,
    limit: int = _ISOLATION_CLEANUP_SCAN_LIMIT,
) -> dict[str, int]:
    """Remove isolation DB records whose workspace directories no longer exist."""
    counts = {
        "worktrees": await _delete_missing_worktree_records(worktree_storage, limit=limit),
        "clones": await _delete_missing_clone_records(clone_storage, limit=limit),
    }
    if counts["worktrees"] or counts["clones"]:
        logger.info(
            "Missing isolation cleanup: removed %s worktree records and %s clone records",
            counts["worktrees"],
            counts["clones"],
        )
    return counts


async def _cleanup_missing_isolation_records_async(
    worktree_storage: LocalWorktreeManager,
    clone_storage: LocalCloneManager,
    *,
    run_db: Callable[..., Awaitable[Any]] | None,
    limit: int = _ISOLATION_CLEANUP_SCAN_LIMIT,
    worktree_delete_executor: WorktreeDeleteExecutor | None = None,
) -> dict[str, int]:
    """Keep each complete guarded metadata deletion off the event loop."""
    worktrees = await _run_db(run_db, worktree_storage.list_worktrees, limit=limit)
    cleanup_pending = await _run_db(run_db, clone_storage.list_cleanup_pending, limit=limit)
    remaining = max(0, limit - len(cleanup_pending))
    clones = cleanup_pending + await _run_db(
        run_db,
        clone_storage.list_clones,
        limit=remaining,
    )
    counts = {"worktrees": 0, "clones": 0}
    for worktree in worktrees:
        counts["worktrees"] += await run_worktree_delete(
            worktree_delete_executor,
            partial(_delete_missing_worktree_record, worktree_storage, worktree.id),
        )
    for clone in clones:
        counts["clones"] += await run_worktree_delete(
            worktree_delete_executor,
            partial(_delete_missing_clone_record, clone_storage, clone.id),
        )
    return counts


async def _delete_missing_worktree_records(
    worktree_storage: LocalWorktreeManager, *, limit: int
) -> int:
    return sum(
        [
            await _delete_missing_worktree_record(worktree_storage, row.id, DestructiveBoundary())
            for row in worktree_storage.list_worktrees(limit=limit)
        ]
    )


async def _delete_missing_worktree_record(
    worktree_storage: LocalWorktreeManager,
    worktree_id: str,
    boundary: DestructiveBoundary,
) -> int:
    expected = await asyncio.to_thread(worktree_storage.get, worktree_id)
    if expected is None or not expected.branch_name or os.path.isdir(expected.worktree_path):
        return 0
    branch_ref = f"refs/heads/{expected.branch_name.removeprefix('refs/heads/')}"
    try:
        root = await asyncio.to_thread(
            require_root, worktree_storage.db, expected.project_id, expected.machine_id
        )
        result = await WorktreeGitManager(root).run_git_command(
            ["show-ref", "--verify", "--quiet", branch_ref], timeout=5
        )
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        logger.debug("Cannot verify missing worktree branch %s", worktree_id, exc_info=True)
        return 0
    if result.returncode != 1:
        return 0
    return await run_thread_to_completion(
        _delete_verified_missing_worktree_record, worktree_storage, expected, boundary
    )


def _delete_verified_missing_worktree_record(
    worktree_storage: LocalWorktreeManager, expected: Worktree, boundary: DestructiveBoundary
) -> int:
    worktree_id = expected.id
    with worktree_storage.lock_for_cleanup(worktree_id, expired_only=False) as current:
        if current is None or (current.worktree_path and os.path.isdir(current.worktree_path)):
            return 0
        if (current.branch_name, current.worktree_path, current.project_id, current.machine_id) != (
            expected.branch_name,
            expected.worktree_path,
            expected.project_id,
            expected.machine_id,
        ):
            return 0
        if not boundary.begin_mutation():
            return 0
        cargo_error = cleanup_checkout_cargo_target_dir(
            Path(current.worktree_path),
            current.project_id,
        )
        if cargo_error is not None:
            logger.warning(
                "Keeping missing worktree record %s after Cargo target cleanup failure: %s",
                current.id,
                cargo_error,
            )
            return 0
        if not worktree_storage.delete(current.id):
            return 0
        logger.info(
            "Removed missing worktree record %s (branch=%s, path=%s)",
            current.id,
            current.branch_name,
            current.worktree_path,
        )
        return 1


async def _delete_missing_clone_records(clone_storage: LocalCloneManager, *, limit: int) -> int:
    cleanup_pending = clone_storage.list_cleanup_pending(limit=limit)
    remaining = max(0, limit - len(cleanup_pending))
    rows = cleanup_pending + clone_storage.list_clones(limit=remaining)
    return sum(
        [
            await _delete_missing_clone_record(clone_storage, row.id, DestructiveBoundary())
            for row in rows
        ]
    )


async def _delete_missing_clone_record(
    clone_storage: LocalCloneManager,
    clone_id: str,
    boundary: DestructiveBoundary,
) -> int:
    return await run_thread_to_completion(
        _delete_missing_clone_record_sync, clone_storage, clone_id, boundary
    )


def _delete_missing_clone_record_sync(
    clone_storage: LocalCloneManager, clone_id: str, boundary: DestructiveBoundary
) -> int:
    with clone_storage.lock_for_cleanup(clone_id, expired_only=False) as current:
        if current is None or (current.clone_path and os.path.isdir(current.clone_path)):
            return 0
        if not boundary.begin_mutation():
            return 0
        cargo_error = cleanup_checkout_cargo_target_dir(
            Path(current.clone_path),
            current.project_id,
        )
        if cargo_error is not None:
            logger.warning(
                "Keeping missing clone record %s after Cargo target cleanup failure: %s",
                current.id,
                cargo_error,
            )
            return 0
        if not clone_storage.delete(current.id):
            return 0
        logger.info(
            "Removed missing clone record %s (branch=%s, path=%s)",
            current.id,
            current.branch_name,
            current.clone_path,
        )
        return 1
