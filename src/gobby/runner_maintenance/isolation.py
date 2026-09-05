"""Worktree, clone, and tmux isolation maintenance."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from gobby.agents.tmux.session_manager import TmuxProbeState
from gobby.clones.git import CloneGitManager, GitOperationResult
from gobby.runner_maintenance.isolation_reconciliation import reconcile_isolation_registry
from gobby.runner_maintenance_helpers import _positive_int_or_default, _run_db
from gobby.runner_tmux_repair import (
    TmuxRepairSessionManager,
    _select_tmux_repair_sessions,
    _tmux_repair_pane_key,
)
from gobby.sessions.tmux_window_naming import (
    enforce_window_name_if_unmanaged,
    probe_tmux_pane,
    release_window_name_if_unowned,
    resolve_tmux_repair_owner,
)
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import require_root
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.terminal_ownership import TERMINAL_TITLE_REPAIR_STATUSES
from gobby.worktrees.deletion import (
    DeletionSurface,
    WorktreeDeletionRequest,
    delete_worktree_transaction,
)
from gobby.worktrees.executor import (
    DestructiveBoundary,
    WorktreeDeleteExecutor,
    run_worktree_delete,
)
from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger("gobby.runner_maintenance")
_ISOLATION_CLEANUP_SCAN_LIMIT = 1000


async def tmux_window_name_repair_loop(
    session_manager: TmuxRepairSessionManager | None,
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 120,
    session_list_limit: int = 200,
) -> None:
    """Ensure active tmux-backed sessions have Gobby-named windows.

    Some interactive sessions — notably Claude Code in a VSCode tmux pane — keep
    an empty title, so the session-start window rename never lands and the tmux
    window name stays frozen at whatever the CLI's startup OSC set (e.g. its
    version string), which then leaks into the VSCode terminal tab via
    ``set-titles-string "#W"``. This sweep renames any active tracked session
    whose tmux window still reports ``automatic-rename=on`` (i.e. Gobby never
    named it), repairing already-stuck windows and self-healing any
    session-start miss. Windows Gobby has already named are skipped.
    """
    normalized_session_list_limit = _positive_int_or_default(session_list_limit, 200)
    normalized_interval_seconds = _positive_int_or_default(interval_seconds, 120)

    async def _repair_once() -> None:
        if session_manager is None:
            return
        try:
            sessions = await asyncio.to_thread(
                session_manager.list,
                statuses=list(TERMINAL_TITLE_REPAIR_STATUSES),
                limit=normalized_session_list_limit,
            )
        except Exception as e:
            logger.warning("tmux window repair: failed to list sessions: %s", e)
            return
        renamed = 0
        missing_sockets: set[tuple[str, str]] = set()
        for session in _select_tmux_repair_sessions(sessions):
            identity = _tmux_repair_pane_key(session)
            if identity is None:
                continue
            machine_id, socket_identity, pane = identity
            if not machine_id:
                continue
            socket_key = machine_id, socket_identity
            if socket_key in missing_sockets:
                continue
            try:
                probe = await probe_tmux_pane(session)
                if probe is None or probe.state is TmuxProbeState.INDETERMINATE:
                    continue
                if probe.state is TmuxProbeState.SERVER_MISSING:
                    missing_sockets.add(socket_key)
                    affected = await asyncio.to_thread(
                        session_manager.expire_tmux_socket_sessions,
                        machine_id,
                        socket_identity,
                    )
                    if affected:
                        logger.info(
                            "tmux window repair: detached %s session(s) from missing server",
                            len(affected),
                            extra={
                                "event": "tmux_server_missing_cleanup",
                                "machine_id": machine_id,
                                "tmux_socket": socket_identity,
                                "affected_count": len(affected),
                            },
                        )
                    continue
                if probe.pane_exists is False:
                    affected = await asyncio.to_thread(
                        session_manager.expire_tmux_pane_sessions,
                        machine_id,
                        socket_identity,
                        pane,
                    )
                    if affected:
                        logger.info(
                            "tmux window repair: detached %s session(s) from missing pane",
                            len(affected),
                            extra={
                                "event": "tmux_pane_missing_cleanup",
                                "machine_id": machine_id,
                                "tmux_socket": socket_identity,
                                "tmux_pane": pane,
                                "affected_count": len(affected),
                            },
                        )
                    continue
                owner = await resolve_tmux_repair_owner(session)
                if owner is not None and await enforce_window_name_if_unmanaged(owner):
                    renamed += 1
                elif owner is None and await release_window_name_if_unowned(session):
                    renamed += 1
            except Exception:
                logger.warning(
                    "tmux window repair: rename failed for session %s",
                    getattr(session, "ref", "?"),
                    exc_info=True,
                )
        if renamed:
            logger.debug("tmux window repair: renamed %s window(s)", renamed)

    # Run once on startup, then loop.
    try:
        await _repair_once()
    except Exception as e:
        logger.error("Error in initial tmux window repair: %s", e)

    while not is_shutdown_requested():
        try:
            await asyncio.sleep(normalized_interval_seconds)
            await _repair_once()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in tmux window repair loop: %s", e)


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
                            resolve_git_manager=lambda current: WorktreeGitManager(
                                require_root(db, current.project_id, current.machine_id)
                            ),
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
                    clone_result = await run_worktree_delete(
                        worktree_delete_executor,
                        partial(_delete_expired_clone, db, clone_storage, clone.id),
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


def _delete_expired_clone(
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
        status = manager.get_clone_status(path)
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
        if manager.run_git_command(["check-ref-format", target_ref], timeout=5).returncode != 0:
            return GitOperationResult(False, "Clone base is not a valid local branch")
        if (
            manager.run_git_command(["symbolic-ref", "--quiet", target_ref], timeout=5).returncode
            != 1
        ):
            return GitOperationResult(False, "Clone base must be a direct local branch")
        head = manager.run_git_command(["rev-parse", "--verify", "HEAD"], cwd=path, timeout=5)
        if head.returncode != 0 or not head.stdout.strip():
            return GitOperationResult(False, "Clone HEAD cannot be verified")
        proof = manager.run_git_command(
            ["merge-base", "--is-ancestor", head.stdout.strip(), target_ref], timeout=10
        )
        if proof.returncode != 0:
            return GitOperationResult(False, "Clone HEAD is not merged into its local base")
        if not boundary.begin_mutation():
            return GitOperationResult(False, "Clone cleanup cancelled before mutation")
        result = manager.delete_clone(path, force=False)
        if result.success and not storage.delete(clone_id):
            return GitOperationResult(False, "Failed to delete clone record")
        return result


def _cleanup_missing_isolation_records(
    worktree_storage: LocalWorktreeManager,
    clone_storage: LocalCloneManager,
    *,
    limit: int = _ISOLATION_CLEANUP_SCAN_LIMIT,
) -> dict[str, int]:
    """Remove isolation DB records whose workspace directories no longer exist."""
    counts = {
        "worktrees": _delete_missing_worktree_records(worktree_storage, limit=limit),
        "clones": _delete_missing_clone_records(clone_storage, limit=limit),
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
    clones = await _run_db(run_db, clone_storage.list_clones, limit=limit)
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


def _delete_missing_worktree_records(worktree_storage: LocalWorktreeManager, *, limit: int) -> int:
    return sum(
        _delete_missing_worktree_record(worktree_storage, row.id, DestructiveBoundary())
        for row in worktree_storage.list_worktrees(limit=limit)
    )


def _delete_missing_worktree_record(
    worktree_storage: LocalWorktreeManager,
    worktree_id: str,
    boundary: DestructiveBoundary,
) -> int:
    with worktree_storage.lock_for_cleanup(worktree_id, expired_only=False) as current:
        if current is None or (current.worktree_path and os.path.isdir(current.worktree_path)):
            return 0
        if not current.branch_name:
            return 0
        branch_ref = f"refs/heads/{current.branch_name.removeprefix('refs/heads/')}"
        try:
            root = require_root(worktree_storage.db, current.project_id, current.machine_id)
            manager = WorktreeGitManager(root)
            result = manager.run_git_command(
                ["show-ref", "--verify", "--quiet", branch_ref], timeout=5
            )
        except (ValueError, OSError, subprocess.SubprocessError):
            logger.debug("Cannot verify missing worktree branch %s", current.id, exc_info=True)
            return 0
        if result.returncode != 1:
            return 0
        if not boundary.begin_mutation():
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


def _delete_missing_clone_records(clone_storage: LocalCloneManager, *, limit: int) -> int:
    return sum(
        _delete_missing_clone_record(clone_storage, row.id, DestructiveBoundary())
        for row in clone_storage.list_clones(limit=limit)
    )


def _delete_missing_clone_record(
    clone_storage: LocalCloneManager,
    clone_id: str,
    boundary: DestructiveBoundary,
) -> int:
    with clone_storage.lock_for_cleanup(clone_id, expired_only=False) as current:
        if current is None or (current.clone_path and os.path.isdir(current.clone_path)):
            return 0
        if not boundary.begin_mutation():
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


def _run_git_command(args: list[str], *, cwd: str) -> int:
    """Run a git command in the recorded project repository."""
    # This helper receives shell-free argv assembled by the isolation reaper.
    import subprocess  # nosec B404

    result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=30)  # nosec B603
    return result.returncode
