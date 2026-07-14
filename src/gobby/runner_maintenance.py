"""Background maintenance tasks for GobbyRunner.

Standalone utilities for metrics, vector store rebuild,
signal handling, and PID file management. Extracted from runner.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from random import SystemRandom
from typing import TYPE_CHECKING, Any

from gobby.cli.utils import get_gobby_home
from gobby.config.bin_freshness import BinFreshnessConfig
from gobby.runner_maintenance_recurring import (
    _wait_for_first_maintenance_cycle,
)
from gobby.runner_maintenance_recurring import (
    memory_reconcile_loop as memory_reconcile_loop,
)
from gobby.runner_maintenance_recurring import (
    metrics_archive_loop as metrics_archive_loop,
)
from gobby.runner_maintenance_recurring import (
    metrics_cleanup_loop as metrics_cleanup_loop,
)
from gobby.runner_tmux_repair import (
    TmuxRepairSessionManager,
)
from gobby.runner_tmux_repair import (
    _select_tmux_repair_sessions as _select_tmux_repair_sessions,
)
from gobby.runner_tmux_repair import (
    _tmux_repair_candidate_score as _tmux_repair_candidate_score,
)
from gobby.runner_tmux_repair import (
    _tmux_repair_pane_key as _tmux_repair_pane_key,
)
from gobby.servers.chat_attachment_files import unlink_stale_attachment_file_sync
from gobby.shutdown_intent import (
    ShutdownIntent,
    ShutdownIntentRecord,
    format_shutdown_source,
    read_shutdown_intent,
    recover_stale_restart_intent,
)
from gobby.workflows.summary_actions import (
    enforce_window_name_if_unmanaged,
    repair_missing_session_title,
)

if TYPE_CHECKING:
    from gobby.memory.vectorstore import VectorStore
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)
_JITTER_RANDOM = SystemRandom()
_ISOLATION_CLEANUP_SCAN_LIMIT = 1000
_CHAT_ATTACHMENT_CLEANUP_BATCH_LIMIT = 500
_COMMS_CLEANUP_BATCH_LIMIT = 500
_SKILL_CLEANUP_BATCH_LIMIT = 500
_APPROVAL_EXPIRY_BATCH_LIMIT = 100
_METRIC_SNAPSHOT_CLEANUP_BATCH_LIMIT = 1000


def _positive_int_or_default(value: Any, default: int) -> int:
    if not isinstance(value, int):
        return default
    return max(1, value)


async def _run_db(
    runner: Callable[..., Awaitable[Any]] | None,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    if runner is None:
        return await asyncio.to_thread(func, *args, **kwargs)
    return await runner(func, *args, **kwargs)


async def _sleep_until_next_bin_freshness_cycle(
    duration: float,
    *,
    is_shutdown_requested: Callable[[], bool],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    if duration <= 0 or is_shutdown_requested():
        return
    await sleep(duration)


async def bin_freshness_loop(
    db: HubDatabase,
    config: BinFreshnessConfig,
    is_shutdown_requested: Callable[[], bool],
    *,
    update_once: Callable[[HubDatabase, BinFreshnessConfig], list[Any]] | None = None,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[float], float] | None = None,
) -> None:
    """Background loop for GitHub-backed managed native binary updates."""
    if not config.enabled:
        return

    from gobby.install.bin_freshness_updater import update_all_managed_bins

    updater = update_once or update_all_managed_bins
    jitter_fn = jitter or (lambda upper: _JITTER_RANDOM.uniform(0, upper))

    try:
        await _sleep_until_next_bin_freshness_cycle(
            config.initial_delay_seconds,
            is_shutdown_requested=is_shutdown_requested,
            sleep=sleep,
        )
        while not is_shutdown_requested():
            try:
                await _run_db(run_db, updater, db, config)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in bin freshness loop: {e}")

            interval = config.interval_seconds
            if config.jitter_seconds > 0:
                interval += jitter_fn(config.jitter_seconds)
            try:
                await _sleep_until_next_bin_freshness_cycle(
                    interval,
                    is_shutdown_requested=is_shutdown_requested,
                    sleep=sleep,
                )
            except asyncio.CancelledError:
                break
    except asyncio.CancelledError:
        pass


async def drain_hook_inbox_loop(
    app: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 60,
) -> None:
    """Replay pending hook inbox envelopes on the maintenance loop."""
    from gobby.hooks.inbox import drain_hook_inbox_loop as _drain_hook_inbox_loop

    await _drain_hook_inbox_loop(
        app,
        is_shutdown_requested,
        interval_seconds=interval_seconds,
    )


async def span_cleanup_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    retention_days: int = 7,
) -> None:
    """Background loop for periodic span cleanup (every 24 hours)."""
    interval_seconds = 24 * 60 * 60  # 24 hours

    from gobby.storage.spans import SpanStorage

    storage = SpanStorage(db)

    while not is_shutdown_requested():
        try:
            deleted = storage.delete_old_spans(retention_days=retention_days)
            if deleted > 0:
                logger.info(f"Periodic span cleanup: removed {deleted} old spans")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in span cleanup loop: {e}")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


async def unmodeled_observation_cleanup_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    retention_days: int = 30,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Background loop for pruning old unmodeled-observation occurrence guards."""
    interval_seconds = 24 * 60 * 60

    from gobby.storage.unmodeled_observations import UnmodeledObservationStore

    store = UnmodeledObservationStore(db)

    while not is_shutdown_requested():
        try:
            deleted = await _run_db(
                run_db,
                store.prune_events_older_than,
                retention_days=retention_days,
            )
            if deleted > 0:
                logger.info(
                    "Periodic unmodeled-observation cleanup: removed %s old occurrence rows",
                    deleted,
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in unmodeled observation cleanup loop: {e}")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


async def rebuild_vector_store(
    vector_store: VectorStore,
    memory_dicts: list[dict[str, str]],
    embed_fn: Any,
) -> None:
    """Rebuild VectorStore index in the background."""
    try:
        await vector_store.rebuild(memory_dicts, embed_fn)
        logger.info("VectorStore rebuild complete")
    except asyncio.CancelledError:
        logger.info("VectorStore rebuild cancelled")
    except Exception as e:
        logger.error(f"VectorStore rebuild failed: {e}")


async def cleanup_zombie_messages_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_hours: int = 6,
    ttl_hours: int = 48,
) -> None:
    """Expire undelivered messages to dead/expired sessions.

    Marks undelivered inter-session messages as delivered when their target
    session has been closed/expired for longer than ``ttl_hours``.  This
    prevents the notify-unread-mail rule from repeatedly nudging a session
    that will never read its mail.
    """
    interval_seconds = interval_hours * 3600

    def _expire_zombies() -> None:
        expired = db.execute(
            "UPDATE inter_session_messages SET delivered_at = CURRENT_TIMESTAMP "
            "WHERE delivered_at IS NULL AND to_session IN ("
            "  SELECT id FROM sessions WHERE status IN ('closed', 'expired') "
            "  AND (updated_at < NOW() - (%s::double precision * INTERVAL '1 hour') "
            "       OR (updated_at IS NULL "
            "           AND created_at < NOW() "
            "               - (%s::double precision * INTERVAL '1 hour')))"
            ")",
            (ttl_hours, ttl_hours),
        )
        if expired.rowcount:
            logger.info(f"Expired {expired.rowcount} zombie messages")

    # Run once immediately on startup, then loop.
    try:
        _expire_zombies()
    except Exception as e:
        logger.error(f"Error in initial zombie message cleanup: {e}")

    while not is_shutdown_requested():
        try:
            await asyncio.sleep(interval_seconds)
            _expire_zombies()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in zombie message cleanup loop: {e}")


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
                statuses=["active", "paused"],
                limit=normalized_session_list_limit,
            )
        except Exception as e:
            logger.warning(f"tmux window repair: failed to list sessions: {e}")
            return
        renamed = 0
        for session in _select_tmux_repair_sessions(sessions):
            try:
                # Land a transcript-derived title first for title-less sessions
                # with turns; persisting it schedules the window rename via the
                # title-change side effect, so skip the empty-title enforce path.
                if await repair_missing_session_title(session_manager, session):
                    renamed += 1
                    continue
                if await enforce_window_name_if_unmanaged(session):
                    renamed += 1
            except Exception:
                logger.warning(
                    "tmux window repair: rename failed for session %s",
                    getattr(session, "ref", "?"),
                    exc_info=True,
                )
        if renamed:
            logger.info(f"tmux window repair: renamed {renamed} window(s)")

    # Run once on startup, then loop.
    try:
        await _repair_once()
    except Exception as e:
        logger.error(f"Error in initial tmux window repair: {e}")

    while not is_shutdown_requested():
        try:
            await asyncio.sleep(normalized_interval_seconds)
            await _repair_once()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in tmux window repair loop: {e}")


async def cleanup_comms_messages_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    retention_days: int = 30,
    *,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    interval_seconds: int = 24 * 60 * 60,
    startup_delay_seconds: float | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    from gobby.communications.attachments import AttachmentManager
    from gobby.storage.communications import LocalCommunicationsStore
    from gobby.storage.inter_session_messages import InterSessionMessageManager

    store = LocalCommunicationsStore(db)
    mailbox_store = InterSessionMessageManager(db)
    attachment_manager = AttachmentManager()
    sleep_fn = sleep or asyncio.sleep

    if not await _wait_for_first_maintenance_cycle(
        "comms-message-cleanup",
        is_shutdown_requested,
        startup_delay_seconds=startup_delay_seconds,
        sleep=sleep_fn,
    ):
        return

    while True:
        try:
            cutoff = datetime.now(UTC) - timedelta(days=retention_days)

            deleted_messages = await _run_db(
                run_db,
                store.delete_messages_before,
                cutoff,
                limit=_COMMS_CLEANUP_BATCH_LIMIT,
            )
            deleted_mailbox_messages = await _run_db(
                run_db,
                mailbox_store.delete_delivered_before,
                cutoff,
                limit=_COMMS_CLEANUP_BATCH_LIMIT,
            )
            deleted_attachments = await asyncio.to_thread(
                attachment_manager.cleanup_old,
                days=retention_days,
                limit=_COMMS_CLEANUP_BATCH_LIMIT,
            )

            if deleted_messages > 0:
                logger.info(f"Comms message cleanup: removed {deleted_messages} old messages")
            if deleted_mailbox_messages > 0:
                logger.info(
                    "Mailbox message cleanup: removed %s old delivered messages",
                    deleted_mailbox_messages,
                )
            if deleted_attachments > 0:
                logger.info(
                    "Comms attachment cleanup: removed %s old local files",
                    deleted_attachments,
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in comms message cleanup loop: {e}")
        try:
            await sleep_fn(interval_seconds)
        except asyncio.CancelledError:
            break
        if is_shutdown_requested():
            break


async def purge_deleted_skills_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    retention_days: int = 30,
    *,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    interval_seconds: int = 24 * 60 * 60,
    startup_delay_seconds: float | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Permanently remove skills whose soft-delete retention period has elapsed."""
    from gobby.storage.skills import LocalSkillManager

    storage = LocalSkillManager(db)
    sleep_fn = sleep or asyncio.sleep

    if not await _wait_for_first_maintenance_cycle(
        "deleted-skill-purge",
        is_shutdown_requested,
        startup_delay_seconds=startup_delay_seconds,
        sleep=sleep_fn,
    ):
        return

    while True:
        try:
            cutoff = datetime.now(UTC) - timedelta(days=retention_days)
            deleted = await _run_db(
                run_db,
                storage.purge_soft_deleted_before,
                cutoff,
                limit=_SKILL_CLEANUP_BATCH_LIMIT,
            )
            if deleted > 0:
                logger.info("Skill retention purge: removed %s soft-deleted skills", deleted)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in deleted skill purge loop: %s", e)
        try:
            await sleep_fn(interval_seconds)
        except asyncio.CancelledError:
            break
        if is_shutdown_requested():
            break


def _remove_stale_chat_attachment_file(local_path: str) -> bool:
    path, removed = unlink_stale_attachment_file_sync(local_path)
    if path is None:
        logger.warning("Skipping stale chat attachment outside managed storage: %s", local_path)
        return False

    # Empty upload directories are scratch structure; pruning is best effort
    # because concurrent uploads may share parent buckets.
    root = get_gobby_home() / "projects"
    current = path.parent
    while current != root and root in current.parents:
        try:
            current.rmdir()
        except FileNotFoundError:
            break
        except OSError:
            break
        current = current.parent
    return removed


async def cleanup_chat_attachments_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    *,
    retention_hours: int = 24,
    interval_minutes: int = 60,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Delete stale unbound chat uploads left behind by abandoned browser drafts."""
    from gobby.storage import chat_attachments

    retention_hours = _positive_int_or_default(retention_hours, 24)
    interval_seconds = _positive_int_or_default(interval_minutes, 60) * 60

    async def cleanup_once() -> None:
        cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
        records = await _run_db(
            run_db,
            chat_attachments.delete_stale_unbound_attachments,
            db,
            cutoff=cutoff,
            limit=_CHAT_ATTACHMENT_CLEANUP_BATCH_LIMIT,
        )
        if not records:
            return
        removed_files = 0
        for record in records:
            if await asyncio.to_thread(_remove_stale_chat_attachment_file, record.local_path):
                removed_files += 1
        logger.info(
            "Removed %s stale unbound chat attachment row(s), %s file(s)",
            len(records),
            removed_files,
        )

    try:
        await cleanup_once()
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"Error in initial chat attachment cleanup: {e}")

    while not is_shutdown_requested():
        try:
            await sleep(interval_seconds)
            await cleanup_once()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in chat attachment cleanup loop: {e}")


async def expire_approval_timeouts_loop(
    pipeline_execution_manager: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 60,
    *,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Expire pipeline steps that have exceeded their approval timeout.

    Runs every ``interval_seconds``, finds steps in waiting_approval whose
    timeout has elapsed, marks them FAILED and their parent execution CANCELLED.
    """
    while not is_shutdown_requested():
        try:
            await asyncio.sleep(interval_seconds)
            expired_steps = await _run_db(
                run_db,
                pipeline_execution_manager.get_expired_approval_steps,
                limit=_APPROVAL_EXPIRY_BATCH_LIMIT,
            )
            for step in expired_steps:
                try:
                    await _run_db(
                        run_db,
                        pipeline_execution_manager.expire_approval_timeout,
                        step_execution_id=step.id,
                        execution_id=step.execution_id,
                    )
                    logger.info(
                        f"Approval timed out for step {step.step_id} "
                        f"in execution {step.execution_id}"
                    )
                except Exception:
                    logger.error(
                        f"Failed to expire approval for step {step.id}",
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in approval timeout loop: {e}")


async def metric_snapshot_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 60,
    retention_hours: int = 24,
    *,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Background loop that snapshots OTel metrics every interval.

    Captures get_all_metrics() output to the PostgreSQL hub for dashboard time-series charts.
    Cleans old snapshots each tick to maintain 24h retention.
    """
    from gobby.storage.metric_snapshots import MetricSnapshotStorage
    from gobby.telemetry.instruments import get_all_metrics, update_daemon_metrics

    storage = MetricSnapshotStorage(db)

    while not is_shutdown_requested():
        try:
            update_daemon_metrics()
            metrics = get_all_metrics()
            await _run_db(run_db, storage.save_snapshot, metrics)
            deleted = await _run_db(
                run_db,
                storage.delete_old_snapshots,
                retention_hours=retention_hours,
                limit=_METRIC_SNAPSHOT_CLEANUP_BATCH_LIMIT,
            )
            if deleted > 0:
                logger.debug(f"Metric snapshot cleanup: removed {deleted} old snapshots")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in metric snapshot loop: {e}")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


async def recall_drift_monitor_loop(
    db: Any,
    memory_config: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: float | None = None,
) -> None:
    """Background recall-quality drift monitor (#17201).

    Each tick replays the recent labeled recall-signal window under the
    effective constants and compares live pairwise accuracy against the
    recorded holdout baseline; ``run_drift_check_from_store`` logs a WARNING
    alarm with the rollback response path when quality regresses beyond the
    configured threshold.
    """
    from gobby.memory.recall_drift import run_drift_check_from_store
    from gobby.storage.recall_signals import RecallSignalStore

    store = RecallSignalStore(db)
    if interval_seconds is None:
        interval_hours = getattr(memory_config, "recall_drift_interval_hours", 24.0)
        interval_seconds = float(interval_hours) * 3600.0

    while not is_shutdown_requested():
        try:
            run_drift_check_from_store(store, memory_config)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in recall drift monitor loop: {e}")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


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
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.worktrees import LocalWorktreeManager

    worktree_storage = LocalWorktreeManager(db)
    clone_storage = LocalCloneManager(db)
    project_storage = LocalProjectManager(db)
    interval_seconds = interval_hours * 3600

    while not is_shutdown_requested():
        try:
            await asyncio.sleep(interval_seconds)

            # Reap expired worktrees
            expired_worktrees = await _run_db(run_db, worktree_storage.find_expired)
            for wt in expired_worktrees:
                try:
                    path = wt.worktree_path
                    project = await _run_db(run_db, project_storage.get, wt.project_id)
                    if project is None or not project.repo_path:
                        raise ValueError(
                            f"Project {wt.project_id} has no repository path for worktree {wt.id}"
                        )
                    repo_path = project.repo_path
                    # Try git worktree remove first, fall back to shutil
                    removed = False
                    try:
                        result = await asyncio.to_thread(
                            _run_git_command,
                            ["git", "worktree", "remove", "--force", path],
                            cwd=repo_path,
                        )
                        removed = result == 0
                        if not removed:
                            logger.warning(
                                "git worktree remove failed for %s in %s (exit code %d)",
                                path,
                                repo_path,
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
                        cwd=repo_path,
                    )
                    if prune_result != 0:
                        logger.warning(
                            "git worktree prune failed in %s (exit code %d)",
                            repo_path,
                            prune_result,
                        )
                    # Delete the branch
                    if wt.branch_name:
                        branch_result = await asyncio.to_thread(
                            _run_git_command,
                            ["git", "branch", "-D", wt.branch_name],
                            cwd=repo_path,
                        )
                        if branch_result != 0:
                            logger.warning(
                                "git branch deletion failed for %s in %s (exit code %d)",
                                wt.branch_name,
                                repo_path,
                                branch_result,
                            )
                    # Remove DB record
                    await _run_db(run_db, worktree_storage.delete, wt.id)
                    logger.info(
                        f"Expired worktree cleanup: deleted {wt.id} "
                        f"(branch={wt.branch_name}, path={path})"
                    )
                except Exception:
                    logger.error(
                        f"Failed to clean up expired worktree {wt.id}",
                        exc_info=True,
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
                        f"Expired clone cleanup: deleted {clone.id} "
                        f"(branch={clone.branch_name}, path={path})"
                    )
                except Exception:
                    logger.error(
                        f"Failed to clean up expired clone {clone.id}",
                        exc_info=True,
                    )

            await _cleanup_missing_isolation_records_async(
                worktree_storage,
                clone_storage,
                run_db=run_db,
            )

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in expired isolation cleanup loop: {e}")


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
    worktrees = await _run_db(run_db, worktree_storage.list_worktrees, limit=limit)
    clones = await _run_db(run_db, clone_storage.list_clones, limit=limit)

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


def write_shutdown_source(
    source: str,
    sender_pid: int | None = None,
    *,
    intent: str | None = None,
) -> None:
    """Write a marker file identifying why/who is sending SIGTERM."""
    try:
        from gobby.shutdown_intent import ShutdownIntent, write_shutdown_intent

        write_shutdown_intent(
            source,
            intent or ShutdownIntent.STOP,
            sender_pid=sender_pid,
            home=get_gobby_home(),
        )
    except Exception as e:
        logger.debug(
            f"Failed to write shutdown source={source} pid={sender_pid or os.getpid()}: {e}",
            exc_info=True,
        )


def setup_signal_handlers(
    shutdown_callback: Callable[[], None],
    shutdown_intent_callback: Callable[[ShutdownIntent], None] | None = None,
) -> None:
    """Register SIGTERM/SIGINT handlers to trigger graceful shutdown."""
    loop = asyncio.get_running_loop()
    recorded_shutdown: ShutdownIntentRecord | None = None

    def _read_signal_shutdown_record() -> ShutdownIntentRecord:
        home = get_gobby_home()
        shutdown_record = read_shutdown_intent(home=home)
        if shutdown_record.stale:
            return recover_stale_restart_intent(
                shutdown_record,
                max_age_seconds=120,
            )
        return shutdown_record

    def _make_handler(sig: signal.Signals) -> Callable[[], None]:
        def handle_shutdown() -> None:
            nonlocal recorded_shutdown

            import traceback

            if recorded_shutdown is None:
                logger.info(
                    f"Received {sig.name} (signal {sig.value}), initiating graceful shutdown... (pid={os.getpid()}, ppid={os.getppid()})",
                )
                # Log stack trace to help identify what triggered the signal
                logger.debug(f"Stack at signal receipt:\n{''.join(traceback.format_stack())}")
                shutdown_record = _read_signal_shutdown_record()
                recorded_shutdown = shutdown_record
                logger.info(f"Shutdown source: {format_shutdown_source(shutdown_record)}")
                if shutdown_intent_callback is not None:
                    try:
                        shutdown_intent_callback(shutdown_record.intent)
                    except Exception:
                        logger.exception("Shutdown intent callback failed")
            else:
                shutdown_record = recorded_shutdown
                logger.debug(
                    "Shutdown already in progress; original source: %s",
                    format_shutdown_source(shutdown_record),
                )
            shutdown_callback()

        return handle_shutdown

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _make_handler(sig))


def cleanup_pid_file() -> None:
    """Remove PID file if it points to our process."""
    try:
        pid_file = get_gobby_home() / "gobby.pid"
        if pid_file.exists():
            stored_pid = int(pid_file.read_text().strip())
            if stored_pid == os.getpid():
                pid_file.unlink(missing_ok=True)
                logger.debug("Cleaned up PID file")
    except Exception as e:
        logger.debug(f"PID file cleanup failed (non-fatal): {e}")
