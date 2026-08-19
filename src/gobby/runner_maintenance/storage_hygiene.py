"""Storage hygiene maintenance loops."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from gobby.paths import get_files_home
from gobby.runner_maintenance_helpers import _positive_int_or_default, _run_db
from gobby.runner_maintenance_recurring import _wait_for_first_maintenance_cycle
from gobby.servers.chat_attachment_cleanup import cleanup_stale_attachments_sync
from gobby.servers.chat_attachment_files import unlink_stale_attachment_file_sync
from gobby.storage.schema_contract import sweep_test_schemas

if TYPE_CHECKING:
    from gobby.config.runtime import RuntimeActiveBundle

logger = logging.getLogger("gobby.runner_maintenance")
_CHAT_ATTACHMENT_CLEANUP_BATCH_LIMIT = 500
_SKILL_CLEANUP_BATCH_LIMIT = 500
_APPROVAL_EXPIRY_BATCH_LIMIT = 100
_TEST_SCHEMA_RETENTION_HOURS = 24
_TEST_SCHEMA_SWEEP_INTERVAL_SECONDS = 60 * 60


def sweep_orphaned_test_schemas(
    database_url: str,
    age_hours: int = _TEST_SCHEMA_RETENTION_HOURS,
) -> None:
    """Delegate abandoned test-schema sweeping to gdaemon."""
    sweep_test_schemas(database_url, age_hours=age_hours)


async def sweep_test_schemas_loop(
    database_url: str | None,
    is_shutdown_requested: Callable[[], bool],
    *,
    interval_seconds: int = _TEST_SCHEMA_SWEEP_INTERVAL_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Sweep abandoned test schemas at startup and periodically thereafter."""
    if not database_url:
        return
    while not is_shutdown_requested():
        try:
            await asyncio.to_thread(sweep_orphaned_test_schemas, database_url)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Failed to sweep orphaned Postgres test schemas")
        try:
            await sleep(interval_seconds)
        except asyncio.CancelledError:
            break


async def purge_deleted_skills_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    *,
    capture_bundle: Callable[[], RuntimeActiveBundle],
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
        config = capture_bundle().snapshot.active
        retention_days = getattr(config.skills, "soft_delete_retention_days", 30)
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


def _remove_stale_chat_attachment_file(
    project_id: str,
    attachment_id: str,
    filename: str,
) -> bool:
    return unlink_stale_attachment_file_sync(project_id, attachment_id, filename)


async def cleanup_chat_attachments_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    *,
    capture_bundle: Callable[[], RuntimeActiveBundle],
    run_db: Callable[..., Awaitable[Any]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Delete stale unbound chat uploads left behind by abandoned browser drafts."""

    async def cleanup_once(retention_hours: int) -> None:
        if get_files_home() is None:
            return
        cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
        records = await asyncio.to_thread(
            cleanup_stale_attachments_sync,
            db,
            cutoff=cutoff,
            limit=_CHAT_ATTACHMENT_CLEANUP_BATCH_LIMIT,
        )
        if not records:
            return
        logger.info(
            "Removed %s stale unbound chat attachment row(s), %s file(s)",
            len(records),
            len(records),
        )

    while not is_shutdown_requested():
        config = capture_bundle().snapshot.active.chat
        retention_hours = _positive_int_or_default(
            getattr(config, "attachment_unbound_retention_hours", 24), 24
        )
        interval_seconds = (
            _positive_int_or_default(getattr(config, "attachment_gc_interval_minutes", 60), 60) * 60
        )
        try:
            await cleanup_once(retention_hours)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in chat attachment cleanup loop: %s", e)
        try:
            await sleep(interval_seconds)
        except asyncio.CancelledError:
            break


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
