"""System cron registration for memory dream."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from gobby.memory.dream.service import run_memory_dream
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.projects import PERSONAL_PROJECT_ID

logger = logging.getLogger(__name__)

MEMORY_DREAM_CRON_JOB_NAME = "gobby:memory-dream"
MEMORY_DREAM_CRON_HANDLER = "memory.dream"
MEMORY_DREAM_CRON_DESCRIPTION = "Scheduled memory dream review and consolidation"

CronHandler = Callable[[CronJob], Awaitable[str]]


class CronRegistrationProtocol(Protocol):
    def register_handler(self, name: str, handler: CronHandler) -> None:
        """Register a cron handler by name."""
        ...


def register_memory_dream_cron(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    memory_manager: Any,
    dream_config: Any,
    llm_service: Any | None = None,
    project_id: str | None = None,
) -> int:
    """Register the memory dream handler and reconcile its single system row."""
    if not getattr(dream_config, "enabled", True):
        existing = cron_storage.get_job_by_name(MEMORY_DREAM_CRON_JOB_NAME)
        if existing and existing.enabled:
            updated = cron_storage.update_job(existing.id, enabled=False, next_run_at=None)
            if updated is None:
                raise RuntimeError(
                    f"Failed to disable system cron job: {MEMORY_DREAM_CRON_JOB_NAME}"
                )
        return 0

    async def _handler(_job: CronJob) -> str:
        result = await run_memory_dream(
            memory_manager=memory_manager,
            dream_config=dream_config,
            llm_service=llm_service,
            project_id=project_id,
        )
        if not isinstance(result, dict):
            raise RuntimeError("memory dream returned non-object result")
        if not result.get("success"):
            raise RuntimeError(str(result.get("error", "memory dream failed")))
        run = result.get("run") if isinstance(result.get("run"), dict) else {}
        summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
        run_id = result.get("run_id") or run.get("id")
        if not run_id:
            raise RuntimeError("memory dream completed without run_id")
        return f"memory dream {run_id} completed: {summary.get('mutations', 0)} mutation(s)"

    cron_executor.register_handler(MEMORY_DREAM_CRON_HANDLER, _handler)
    _ensure_system_job(cron_storage, dream_config, project_id)
    return 1


def _ensure_system_job(
    cron_storage: CronJobStorage,
    dream_config: Any,
    project_id: str | None,
) -> None:
    existing = cron_storage.get_job_by_name(MEMORY_DREAM_CRON_JOB_NAME)
    cron_expr = str(getattr(dream_config, "schedule_cron", "0 3 * * *"))
    target_project_id = project_id or PERSONAL_PROJECT_ID
    if existing is None:
        cron_storage.create_job(
            project_id=target_project_id,
            name=MEMORY_DREAM_CRON_JOB_NAME,
            description=MEMORY_DREAM_CRON_DESCRIPTION,
            schedule_type="cron",
            cron_expr=cron_expr,
            action_type="handler",
            action_config={"handler": MEMORY_DREAM_CRON_HANDLER},
            enabled=True,
            is_system=True,
        )
        logger.info("Created system cron job: %s", MEMORY_DREAM_CRON_JOB_NAME)
        return

    if not existing.is_system:
        cron_storage.mark_as_system_job(existing.id)
    repaired = cron_storage.reconcile_system_job_definition(
        existing.id,
        action_type="handler",
        action_config={"handler": MEMORY_DREAM_CRON_HANDLER},
        description=MEMORY_DREAM_CRON_DESCRIPTION,
        schedule_type="cron",
        cron_expr=cron_expr,
        interval_seconds=None,
        run_at=None,
    )
    if repaired and not repaired.enabled:
        cron_storage.toggle_job(repaired.id)
