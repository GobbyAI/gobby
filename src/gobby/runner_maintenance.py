"""Background maintenance tasks for GobbyRunner.

Standalone utilities for tmux cleanup, metrics, vector store rebuild,
signal handling, and PID file management. Extracted from runner.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.cli.utils import get_gobby_home

if TYPE_CHECKING:
    from gobby.mcp_proxy.metrics import ToolMetricsManager
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)


async def cleanup_stale_tmux_sessions() -> None:
    """Kill tmux sessions on the gobby socket not backed by a registered agent."""
    try:
        from gobby.agents.registry import get_running_agent_registry
        from gobby.agents.tmux.session_manager import TmuxSessionManager

        mgr = TmuxSessionManager()
        if not mgr.is_available():
            return

        sessions = await mgr.list_sessions()
        if not sessions:
            return

        registry = get_running_agent_registry()
        registered_names = {a.tmux_session_name for a in registry.list_all() if a.tmux_session_name}

        for session in sessions:
            if session.name not in registered_names:
                logger.info(f"Cleaning up stale tmux session: {session.name}")
                await mgr.kill_session(session.name)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f"Stale tmux session cleanup failed: {e}")


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
            "Failed to write shutdown source=%s pid=%d: %s",
            source,
            sender_pid or os.getpid(),
            e,
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
                "Received %s (signal %d), initiating graceful shutdown... (pid=%d, ppid=%d)",
                sig.name,
                sig.value,
                os.getpid(),
                os.getppid(),
            )
            # Log stack trace to help identify what triggered the signal
            logger.debug("Stack at signal receipt:\n%s", "".join(traceback.format_stack()))
            logger.info("Shutdown source: %s", read_shutdown_source())
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
