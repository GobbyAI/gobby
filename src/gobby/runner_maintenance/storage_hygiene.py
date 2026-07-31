"""Storage hygiene maintenance loops."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.cli.utils import get_gobby_home
from gobby.runner_maintenance_helpers import _positive_int_or_default, _run_db
from gobby.runner_maintenance_recurring import _wait_for_first_maintenance_cycle
from gobby.servers.chat_attachment_files import unlink_stale_attachment_file_sync

logger = logging.getLogger("gobby.runner_maintenance")
_CHAT_ATTACHMENT_CLEANUP_BATCH_LIMIT = 500
_SKILL_CLEANUP_BATCH_LIMIT = 500
_APPROVAL_EXPIRY_BATCH_LIMIT = 100


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
        logger.error("Error in initial chat attachment cleanup: %s", e)

    while not is_shutdown_requested():
        try:
            await sleep(interval_seconds)
            await cleanup_once()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in chat attachment cleanup loop: %s", e)


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
                        "Approval timed out for step %s in execution %s",
                        step.step_id,
                        step.execution_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to expire approval for step %s",
                        step.id,
                    )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in approval timeout loop: %s", e)
