"""Runner shutdown and lifecycle maintenance."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.cli.utils import get_gobby_home
from gobby.shutdown_intent import (
    ShutdownIntent,
    ShutdownIntentRecord,
    format_shutdown_source,
    read_shutdown_intent,
    recover_stale_restart_intent,
)

if TYPE_CHECKING:
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger("gobby.runner_maintenance")


async def rebuild_vector_store(
    vector_store: VectorStore,
    memory_dicts: list[dict[str, str]] | Callable[[], list[dict[str, str]]],
    embed_fn: Any,
) -> None:
    """Rebuild VectorStore index in the background."""
    try:
        rebuild_from_supplier = getattr(vector_store, "rebuild_from_supplier", None)
        if callable(memory_dicts) and callable(rebuild_from_supplier):
            await rebuild_from_supplier(memory_dicts, embed_fn)
        else:
            resolved_memories = (
                await asyncio.to_thread(memory_dicts) if callable(memory_dicts) else memory_dicts
            )
            await vector_store.rebuild(resolved_memories, embed_fn)
        logger.info("VectorStore rebuild complete")
    except asyncio.CancelledError:
        logger.info("VectorStore rebuild cancelled")
    except Exception as e:
        logger.error("VectorStore rebuild failed: %s", e)


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
            "Failed to write shutdown source=%s pid=%s: %s",
            source,
            sender_pid or os.getpid(),
            e,
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
        shutdown_record = read_shutdown_intent(home=home, consume=False)
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
                    "Received %s (signal %s), initiating graceful shutdown... (pid=%s, ppid=%s)",
                    sig.name,
                    sig.value,
                    os.getpid(),
                    os.getppid(),
                )
                # Log stack trace to help identify what triggered the signal
                logger.debug("Stack at signal receipt:\n%s", "".join(traceback.format_stack()))
                shutdown_record = _read_signal_shutdown_record()
                recorded_shutdown = shutdown_record
                logger.info("Shutdown source: %s", format_shutdown_source(shutdown_record))
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
        logger.debug("PID file cleanup failed (non-fatal): %s", e)
