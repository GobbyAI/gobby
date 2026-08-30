"""Worktree, clone, and tmux isolation maintenance."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from gobby.agents.tmux.session_manager import TmuxProbeState
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
from gobby.terminal_ownership import TERMINAL_TITLE_REPAIR_STATUSES

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
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_hours: int = 1,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Reap expired worktrees and clones whose cleanup_after window has passed.

    After a successful merge, worktrees/clones get a 7-day grace period
    (cleanup_after). Once that expires, this loop deletes the directory,
    git branch (worktrees only), and database record.
    """
    import shutil

    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.project_checkouts import require_root
    from gobby.storage.worktrees import LocalWorktreeManager

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
                    path = wt.worktree_path
                    checkout_root = await _run_db(
                        run_db,
                        require_root,
                        db,
                        wt.project_id,
                        wt.machine_id,
                    )
                    # Try git worktree remove first, fall back to shutil
                    removed = False
                    try:
                        result = await asyncio.to_thread(
                            _run_git_command,
                            ["git", "worktree", "remove", "--force", path],
                            cwd=checkout_root,
                        )
                        removed = result == 0
                        if not removed:
                            logger.warning(
                                "git worktree remove failed for %s in %s (exit code %d)",
                                path,
                                checkout_root,
                                result,
                            )
                    except Exception as e:
                        logger.debug("git worktree remove failed for %s: %s", path, e)
                    if not removed and await asyncio.to_thread(os.path.exists, path):
                        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
                    # Prune stale worktree references
                    prune_result = await asyncio.to_thread(
                        _run_git_command,
                        ["git", "worktree", "prune"],
                        cwd=checkout_root,
                    )
                    if prune_result != 0:
                        logger.warning(
                            "git worktree prune failed in %s (exit code %d)",
                            checkout_root,
                            prune_result,
                        )
                    # Delete the branch
                    if wt.branch_name:
                        branch_result = await asyncio.to_thread(
                            _run_git_command,
                            ["git", "branch", "-D", wt.branch_name],
                            cwd=checkout_root,
                        )
                        if branch_result != 0:
                            logger.warning(
                                "git branch deletion failed for %s in %s (exit code %d)",
                                wt.branch_name,
                                checkout_root,
                                branch_result,
                            )
                    # Remove DB record
                    await _run_db(run_db, worktree_storage.delete, wt.id)
                    logger.info(
                        "Expired worktree cleanup: deleted %s (branch=%s, path=%s)",
                        wt.id,
                        wt.branch_name,
                        path,
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
                    path = clone.clone_path
                    if await asyncio.to_thread(os.path.exists, path):
                        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
                    await _run_db(run_db, clone_storage.delete, clone.id)
                    logger.info(
                        "Expired clone cleanup: deleted %s (branch=%s, path=%s)",
                        clone.id,
                        clone.branch_name,
                        path,
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
            )
            await reconcile_isolation_registry(db, run_db=run_db)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in expired isolation cleanup loop: %s", e)


def _cleanup_missing_isolation_records(
    worktree_storage: Any,
    clone_storage: Any,
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
    worktree_storage: Any,
    clone_storage: Any,
    *,
    run_db: Callable[..., Awaitable[Any]] | None,
    limit: int = _ISOLATION_CLEANUP_SCAN_LIMIT,
) -> dict[str, int]:
    """Async missing-record cleanup that keeps path checks off the DB executor."""
    # Storage list methods machine-scope unconditionally via require_machine_id().
    worktrees = await _run_db(
        run_db,
        worktree_storage.list_worktrees,
        limit=limit,
    )
    clones = await _run_db(
        run_db,
        clone_storage.list_clones,
        limit=limit,
    )

    removed_worktrees = 0
    for worktree in worktrees:
        path = worktree.worktree_path
        if path and await asyncio.to_thread(os.path.isdir, path):
            continue
        if await _run_db(run_db, worktree_storage.delete, worktree.id):
            removed_worktrees += 1
            logger.info(
                "Removed missing worktree record %s (branch=%s, path=%s)",
                worktree.id,
                worktree.branch_name,
                path,
            )

    removed_clones = 0
    for clone in clones:
        path = clone.clone_path
        if path and await asyncio.to_thread(os.path.isdir, path):
            continue
        if await _run_db(run_db, clone_storage.delete, clone.id):
            removed_clones += 1
            logger.info(
                "Removed missing clone record %s (branch=%s, path=%s)",
                clone.id,
                clone.branch_name,
                path,
            )

    counts = {"worktrees": removed_worktrees, "clones": removed_clones}
    if counts["worktrees"] or counts["clones"]:
        logger.info(
            "Missing isolation cleanup: removed %s worktree records and %s clone records",
            counts["worktrees"],
            counts["clones"],
        )
    return counts


def _delete_missing_worktree_records(worktree_storage: Any, *, limit: int) -> int:
    removed = 0
    for worktree in worktree_storage.list_worktrees(limit=limit):
        path = worktree.worktree_path
        if path and os.path.isdir(path):
            continue
        if worktree_storage.delete(worktree.id):
            removed += 1
            logger.info(
                "Removed missing worktree record %s (branch=%s, path=%s)",
                worktree.id,
                worktree.branch_name,
                path,
            )
    return removed


def _delete_missing_clone_records(clone_storage: Any, *, limit: int) -> int:
    removed = 0
    for clone in clone_storage.list_clones(limit=limit):
        path = clone.clone_path
        if path and os.path.isdir(path):
            continue
        if clone_storage.delete(clone.id):
            removed += 1
            logger.info(
                "Removed missing clone record %s (branch=%s, path=%s)",
                clone.id,
                clone.branch_name,
                path,
            )
    return removed


def _run_git_command(args: list[str], *, cwd: str) -> int:
    """Run a git command in the recorded project repository."""
    # This helper receives shell-free argv assembled by the isolation reaper.
    import subprocess  # nosec B404

    result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=30)  # nosec B603
    return result.returncode
