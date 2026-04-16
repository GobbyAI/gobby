"""Background maintenance tasks for GobbyRunner.

Standalone utilities for metrics, vector store rebuild,
signal handling, and PID file management. Extracted from runner.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from gobby.cli.utils import get_gobby_home

if TYPE_CHECKING:
    from gobby.mcp_proxy.metrics import ToolMetricsManager
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)


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


async def metrics_cleanup_loop(
    metrics_manager: ToolMetricsManager,
    is_shutdown_requested: Callable[[], bool],
) -> None:
    """Background loop for periodic metrics cleanup (every 24 hours)."""
    interval_seconds = 24 * 60 * 60  # 24 hours

    while not is_shutdown_requested():
        try:
            await asyncio.sleep(interval_seconds)
            deleted = metrics_manager.cleanup_old_metrics()
            if deleted > 0:
                logger.info(f"Periodic metrics cleanup: removed {deleted} old entries")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in metrics cleanup loop: {e}")


async def metrics_archive_loop(
    event_store: Any,
    is_shutdown_requested: Callable[[], bool],
    retention_days: int = 30,
) -> None:
    """Background loop for archiving old metrics events (every 24 hours)."""
    interval_seconds = 24 * 60 * 60  # 24 hours

    while not is_shutdown_requested():
        try:
            await asyncio.sleep(interval_seconds)
            archived = event_store.archive_old_events(retention_days=retention_days)
            if archived > 0:
                logger.info(
                    f"Metrics archive: rolled up {archived} events older than {retention_days} days"
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in metrics archive loop: {e}")


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


async def memory_reconcile_loop(
    memory_manager: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 24 * 60 * 60,
) -> None:
    """Background loop for periodic Qdrant/Neo4j orphan reconciliation (every 24 hours)."""
    while not is_shutdown_requested():
        try:
            await asyncio.sleep(interval_seconds)
            report = await memory_manager.reconcile_stores(dry_run=False)
            qdrant_orphans = report.get("qdrant", {}).get("orphans_deleted", 0)
            neo4j_orphans = report.get("neo4j", {}).get("orphan_memories_deleted", 0)
            neo4j_entities = report.get("neo4j", {}).get("orphan_entities_deleted", 0)
            if qdrant_orphans or neo4j_orphans or neo4j_entities:
                logger.info(
                    f"Memory reconciliation: {qdrant_orphans} Qdrant orphans, "
                    f"{neo4j_orphans} Neo4j memory orphans, "
                    f"{neo4j_entities} Neo4j entity orphans cleaned"
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in memory reconcile loop: {e}")


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
            "UPDATE inter_session_messages SET delivered_at = datetime('now') "
            "WHERE delivered_at IS NULL AND to_session IN ("
            "  SELECT id FROM sessions WHERE status IN ('closed', 'expired') "
            "  AND (updated_at < datetime('now', ? || ' hours')"
            "       OR (updated_at IS NULL AND created_at < datetime('now', ? || ' hours')))"
            ")",
            (f"-{ttl_hours}", f"-{ttl_hours}"),
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


async def cleanup_comms_messages_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    retention_days: int = 30,
) -> None:
    from gobby.storage.communications import LocalCommunicationsStore

    interval_seconds = 24 * 60 * 60

    store = LocalCommunicationsStore(db)

    while not is_shutdown_requested():
        try:
            await asyncio.sleep(interval_seconds)
            cutoff = datetime.now(UTC) - timedelta(days=retention_days)

            deleted_messages = store.delete_messages_before(cutoff)

            if deleted_messages > 0:
                logger.info(f"Comms message cleanup: removed {deleted_messages} old messages")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in comms message cleanup loop: {e}")


async def expire_approval_timeouts_loop(
    pipeline_execution_manager: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 60,
) -> None:
    """Expire pipeline steps that have exceeded their approval timeout.

    Runs every ``interval_seconds``, finds steps in waiting_approval whose
    timeout has elapsed, marks them FAILED and their parent execution CANCELLED.
    """
    from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

    while not is_shutdown_requested():
        try:
            await asyncio.sleep(interval_seconds)
            expired_steps = pipeline_execution_manager.get_expired_approval_steps()
            for step in expired_steps:
                try:
                    pipeline_execution_manager.update_step_execution(
                        step_execution_id=step.id,
                        status=StepStatus.FAILED,
                        error="Approval timed out",
                    )
                    pipeline_execution_manager.update_execution_status(
                        execution_id=step.execution_id,
                        status=ExecutionStatus.CANCELLED,
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
) -> None:
    """Background loop that snapshots OTel metrics every interval.

    Captures get_all_metrics() output to SQLite for dashboard time-series charts.
    Cleans old snapshots each tick to maintain 24h retention.
    """
    from gobby.storage.metric_snapshots import MetricSnapshotStorage
    from gobby.telemetry.instruments import get_all_metrics, update_daemon_metrics

    storage = MetricSnapshotStorage(db)

    while not is_shutdown_requested():
        try:
            update_daemon_metrics()
            metrics = get_all_metrics()
            storage.save_snapshot(metrics)
            deleted = storage.delete_old_snapshots(retention_hours=retention_hours)
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


async def cleanup_expired_isolation_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_hours: int = 1,
) -> None:
    """Reap expired worktrees and clones whose cleanup_after window has passed.

    After a successful merge, worktrees/clones get a 7-day grace period
    (cleanup_after). Once that expires, this loop deletes the directory,
    git branch (worktrees only), and database record.
    """
    import shutil

    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.worktrees import LocalWorktreeManager

    worktree_storage = LocalWorktreeManager(db)
    clone_storage = LocalCloneManager(db)
    interval_seconds = interval_hours * 3600

    while not is_shutdown_requested():
        try:
            await asyncio.sleep(interval_seconds)

            # Reap expired worktrees
            expired_worktrees = worktree_storage.find_expired()
            for wt in expired_worktrees:
                try:
                    path = wt.worktree_path
                    # Try git worktree remove first, fall back to shutil
                    removed = False
                    try:
                        result = await asyncio.to_thread(
                            _run_git_command,
                            ["git", "worktree", "remove", "--force", path],
                        )
                        removed = result == 0
                    except Exception as e:
                        logger.debug("git worktree remove failed for %s: %s", path, e)
                    if not removed and os.path.exists(path):
                        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
                    # Prune stale worktree references
                    await asyncio.to_thread(_run_git_command, ["git", "worktree", "prune"])
                    # Delete the branch
                    if wt.branch_name:
                        await asyncio.to_thread(
                            _run_git_command,
                            ["git", "branch", "-D", wt.branch_name],
                        )
                    # Remove DB record
                    worktree_storage.delete(wt.id)
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
            expired_clones = clone_storage.find_expired()
            for clone in expired_clones:
                try:
                    path = clone.clone_path
                    if os.path.exists(path):
                        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
                    clone_storage.delete(clone.id)
                    logger.info(
                        f"Expired clone cleanup: deleted {clone.id} "
                        f"(branch={clone.branch_name}, path={path})"
                    )
                except Exception:
                    logger.error(
                        f"Failed to clean up expired clone {clone.id}",
                        exc_info=True,
                    )

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in expired isolation cleanup loop: {e}")


def _run_git_command(args: list[str]) -> int:
    """Run a git command and return the exit code."""
    import subprocess

    result = subprocess.run(args, capture_output=True, timeout=30)  # noqa: S603
    return result.returncode


def write_shutdown_source(source: str, sender_pid: int | None = None) -> None:
    """Write a marker file identifying why/who is sending SIGTERM."""
    try:
        data = {
            "source": source,
            "sender_pid": sender_pid or os.getpid(),
            "timestamp": time.time(),
        }
        (get_gobby_home() / "shutdown_source.json").write_text(json.dumps(data))
    except Exception as e:
        logger.debug(
            f"Failed to write shutdown source={source} pid={sender_pid or os.getpid()}: {e}",
            exc_info=True,
        )


def read_shutdown_source() -> str:
    """Read and remove the shutdown source marker. Returns description string."""
    source_file = get_gobby_home() / "shutdown_source.json"
    try:
        if source_file.exists():
            data = json.loads(source_file.read_text())
            source_file.unlink(missing_ok=True)
            age = time.time() - data.get("timestamp", 0)
            if age < 10:  # Only trust if written within last 10 seconds
                return f"source={data['source']}, sender_pid={data.get('sender_pid')}"
            return f"stale shutdown_source.json (age={age:.1f}s): {data}"
        return "unknown (no shutdown_source.json — external SIGTERM)"
    except Exception as e:
        return f"unknown (error reading shutdown_source.json: {e})"


def setup_signal_handlers(shutdown_callback: Callable[[], None]) -> None:
    """Register SIGTERM/SIGINT handlers to trigger graceful shutdown."""
    loop = asyncio.get_running_loop()

    def _make_handler(sig: signal.Signals) -> Callable[[], None]:
        def handle_shutdown() -> None:
            import traceback

            logger.info(
                f"Received {sig.name} (signal {sig.value}), initiating graceful shutdown... (pid={os.getpid()}, ppid={os.getppid()})",
            )
            # Log stack trace to help identify what triggered the signal
            logger.debug(f"Stack at signal receipt:\n{''.join(traceback.format_stack())}")
            logger.info(f"Shutdown source: {read_shutdown_source()}")
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
