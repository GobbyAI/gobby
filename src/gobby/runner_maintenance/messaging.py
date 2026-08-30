"""Messaging maintenance loops."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.runner_maintenance_helpers import _run_db
from gobby.runner_maintenance_recurring import _wait_for_first_maintenance_cycle

logger = logging.getLogger("gobby.runner_maintenance")
_COMMS_CLEANUP_BATCH_LIMIT = 500


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


async def hook_quarantine_retention_loop(
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 3600,
) -> None:
    """Prune expired hook inbox quarantine files on the maintenance loop."""
    from gobby.hooks.inbox import hook_quarantine_retention_loop as _hook_quarantine_retention_loop

    await _hook_quarantine_retention_loop(
        is_shutdown_requested,
        interval_seconds=interval_seconds,
    )


async def hook_receipt_retention_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 3600,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Prune expired receipt-effects rows on the maintenance loop."""
    from gobby.storage.hook_receipts import prune_hook_receipts

    try:
        await _run_db(run_db, prune_hook_receipts, db)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Initial hook receipt prune failed: %s", exc)

    while not is_shutdown_requested():
        try:
            await asyncio.sleep(interval_seconds)
            result = await _run_db(run_db, prune_hook_receipts, db)
            deleted = getattr(result, "deleted", 0)
            if deleted:
                logger.info(
                    "Pruned %s expired hook receipt(s)",
                    deleted,
                    extra={
                        "event": "hook_receipt_pruned",
                        "examined": getattr(result, "examined", deleted),
                        "deleted": deleted,
                        "backlog_remaining": getattr(result, "truncated", False),
                    },
                )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Hook receipt prune loop failed: %s", exc)


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
            logger.info("Expired %s zombie messages", expired.rowcount)

    # Run once immediately on startup, then loop.
    try:
        _expire_zombies()
    except Exception as e:
        logger.error("Error in initial zombie message cleanup: %s", e)

    while not is_shutdown_requested():
        try:
            await asyncio.sleep(interval_seconds)
            _expire_zombies()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in zombie message cleanup loop: %s", e)


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

            deleted_messages, attachment_paths = await _run_db(
                run_db,
                store.delete_messages_before,
                cutoff,
                limit=_COMMS_CLEANUP_BATCH_LIMIT,
            )
            deleted_attachment_paths = await asyncio.to_thread(
                attachment_manager.delete_paths,
                attachment_paths,
            )
            deleted_mailbox_messages = await _run_db(
                run_db,
                mailbox_store.delete_delivered_before,
                cutoff,
                limit=_COMMS_CLEANUP_BATCH_LIMIT,
            )
            deleted_old_attachments = await asyncio.to_thread(
                attachment_manager.cleanup_old,
                days=retention_days,
                limit=_COMMS_CLEANUP_BATCH_LIMIT,
            )

            if deleted_messages > 0:
                logger.info("Comms message cleanup: removed %s old messages", deleted_messages)
            if deleted_attachment_paths > 0:
                logger.info(
                    "Comms attachment cleanup: removed %s files for retained messages",
                    deleted_attachment_paths,
                )
            if deleted_mailbox_messages > 0:
                logger.info(
                    "Mailbox message cleanup: removed %s old delivered messages",
                    deleted_mailbox_messages,
                )
            if deleted_old_attachments > 0:
                logger.info(
                    "Comms attachment cleanup: removed %s old local files",
                    deleted_old_attachments,
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in comms message cleanup loop: %s", e)
        try:
            await sleep_fn(interval_seconds)
        except asyncio.CancelledError:
            break
        if is_shutdown_requested():
            break
