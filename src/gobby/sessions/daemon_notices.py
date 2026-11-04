"""Daemon-owned restart notices: a hold before a restart stops the daemon, DAEMON BACK after.

The daemon sends both as system-sender globals with live wakes, so no session composes
them by hand and idle sessions waiting on DAEMON BACK wake when it arrives.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from gobby.sessions.mailbox import MailboxService
from gobby.shutdown_intent import ShutdownIntent, read_active_shutdown_intent
from gobby.storage.hub.operation_deadline import database_operation_deadline
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import system_session_id

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)

HOLD_NOTICE = (
    "DAEMON HOLD: a daemon restart or cutover is in progress. "
    "gobby-sessions:get_handoff stays allowed and is required: consume a pending handoff now, "
    "without waiting for DAEMON BACK. gobby-agents:wait_for_coordination and "
    "gobby-agents:send_message stay allowed. Pause other task work until DAEMON BACK."
)
BACK_NOTICE = "DAEMON BACK: the daemon is healthy. Resume task work."
NOTICE_MESSAGE_TYPE = "daemon_notice"
# Bounds persistence and wake dispatch so a stalled hub or slow terminal never
# stalls shutdown or startup.
NOTICE_TIMEOUT_SECONDS = 5.0


async def send_daemon_notice(
    runner: GobbyRunner, content: str, *, timeout_seconds: float = NOTICE_TIMEOUT_SECONDS
) -> None:
    """Send one system global with live wakes; failures are logged, never raised."""
    database: Any = getattr(runner, "database", None)
    session_manager = getattr(runner, "session_manager", None)
    if database is None or session_manager is None:
        logger.debug("Skipping daemon notice; session storage unavailable")
        return
    mailbox = MailboxService(
        db=database,
        message_manager=InterSessionMessageManager(database),
        session_manager=session_manager,
        wake_dispatcher=getattr(runner, "wake_dispatcher", None),
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            # Persistence is synchronous, so it runs in a worker under a database
            # deadline: a stalled pool wait or lock then neither blocks the event loop
            # nor outlives the bound, and the deadline frees the worker on time.
            with database_operation_deadline(timeout_seconds=timeout_seconds):
                result = await asyncio.to_thread(
                    mailbox.persist,
                    from_session_id=system_session_id(),
                    target="global",
                    content=content,
                    wake=True,
                    message_type=NOTICE_MESSAGE_TYPE,
                )
            result.wake_results = await mailbox.dispatch_wakes(result)
    except TimeoutError:
        logger.warning("Daemon notice exceeded %.1fs", timeout_seconds)
        return
    except Exception:
        logger.warning("Daemon notice failed", exc_info=True)
        return
    logger.info(
        "Daemon notice sent: recipients=%d error_code=%s",
        len(result.recipient_session_ids),
        result.error_code,
    )


def restart_requested(runner: GobbyRunner) -> bool:
    """Whether this shutdown replaces the daemon: a restart or a cutover's stop."""
    if getattr(runner, "_shutdown_intent", None) is ShutdownIntent.RESTART:
        return True
    record = read_active_shutdown_intent()
    return record is not None and record.intent is ShutdownIntent.RESTART


async def send_hold_notice_if_restarting(runner: GobbyRunner) -> None:
    if restart_requested(runner):
        await send_daemon_notice(runner, HOLD_NOTICE)
