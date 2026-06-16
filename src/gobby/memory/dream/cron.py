"""System cron registration for memory dream."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from gobby.config.persistence import MemoryDreamConfig
from gobby.memory.dream.protocols import MemoryDreamLLMProtocol, MemoryDreamManagerProtocol
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
    memory_manager: MemoryDreamManagerProtocol,
    dream_config: MemoryDreamConfig,
    llm_service: MemoryDreamLLMProtocol | None = None,
    project_id: str | None = None,
    daemon_config: Any = None,
) -> int:
    """Register the memory dream handler and reconcile its single system row."""
    if not dream_config.enabled:
        existing = cron_storage.get_job_by_name(MEMORY_DREAM_CRON_JOB_NAME)
        if existing and existing.enabled:
            updated = cron_storage.update_job(existing.id, enabled=False, next_run_at=None)
            if updated is None:
                logger.warning(
                    "System cron job already disappeared during disable: %s",
                    MEMORY_DREAM_CRON_JOB_NAME,
                )
        return 0

    async def _handler(_job: CronJob) -> str:
        result = await run_memory_dream(
            memory_manager=memory_manager,
            dream_config=dream_config,
            llm_service=llm_service,
            daemon_config=daemon_config,
            project_id=project_id,
        )
        if not isinstance(result, dict):
            raise RuntimeError("memory dream returned non-object result")
        if not result.get("success"):
            raise RuntimeError(str(result.get("error", "memory dream failed")))
        raw_run = result.get("run")
        run = raw_run if isinstance(raw_run, dict) else {}
        raw_summary = run.get("summary")
        summary = raw_summary if isinstance(raw_summary, dict) else {}
        run_id = result.get("run_id")
        if run_id is None:
            run_id = run.get("id")
        if run_id is None:
            raise RuntimeError("memory dream completed without run_id")
        raw_mutations = summary.get("mutations", 0)
        try:
            mutations = int(raw_mutations)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid memory dream mutation count: value=%r type=%s",
                raw_mutations,
                type(raw_mutations).__name__,
            )
            mutations = 0
        return f"memory dream {run_id} completed: {mutations} mutation(s)"

    cron_executor.register_handler(MEMORY_DREAM_CRON_HANDLER, _handler)
    _ensure_system_job(cron_storage, dream_config, project_id)
    return 1


def _ensure_system_job(
    cron_storage: CronJobStorage,
    dream_config: MemoryDreamConfig,
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
    was_enabled = existing.enabled
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
    if repaired and was_enabled and not repaired.enabled:
        cron_storage.toggle_job(repaired.id)
