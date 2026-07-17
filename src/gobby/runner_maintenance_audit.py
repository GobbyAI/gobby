"""Periodic workflow-audit retention maintenance."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from gobby.runner_maintenance_helpers import _run_db
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_audit import WorkflowAuditManager

logger = logging.getLogger(__name__)


async def workflow_audit_cleanup_loop(
    db: HubDatabase,
    is_shutdown_requested: Callable[[], bool],
    *,
    retention_days: int,
    interval_seconds: int = 24 * 60 * 60,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Prune expired workflow-audit rows once per maintenance cycle."""
    audit_manager = WorkflowAuditManager(db)
    while not is_shutdown_requested():
        try:
            deleted = await _run_db(
                run_db,
                audit_manager.cleanup_old_entries,
                days=retention_days,
            )
            if deleted > 0:
                logger.info("Periodic workflow-audit cleanup removed %s old entries", deleted)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in workflow-audit cleanup loop")

        try:
            await sleep(interval_seconds)
        except asyncio.CancelledError:
            break
