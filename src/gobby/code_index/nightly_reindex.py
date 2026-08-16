"""Nightly full code-index reindex with projection sync."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from gobby.code_index.maintenance_log import log_gcode_maintenance_event
from gobby.scheduler.executor import CronHandler
from gobby.storage.cron import CronJobStorage, compute_next_run
from gobby.storage.cron_models import CronJob
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.utils.datetime import resolve_local_timezone

if TYPE_CHECKING:
    from gobby.code_index.context import CodeIndexContext
    from gobby.config.code_index import CodeIndexConfig

logger = logging.getLogger(__name__)

CODE_INDEX_NIGHTLY_REINDEX_JOB_NAME = "gobby:code-index-nightly-full-reindex"
CODE_INDEX_NIGHTLY_REINDEX_HANDLER = "code-index:nightly-full-reindex"
CODE_INDEX_NIGHTLY_REINDEX_DESCRIPTION = (
    "Nightly full code-index reindex with graph and vector projection sync"
)


class CronRegistrationProtocol(Protocol):
    def register_handler(self, name: str, handler: CronHandler) -> None: ...


class CodeIndexNightlyFullReindexer:
    """Runs one full gcode reindex pass across all indexed projects."""

    def __init__(self, context: CodeIndexContext) -> None:
        self._context = context
        self._running = False

    async def run_once(self) -> str:
        if self._running:
            return "Code index nightly full reindex skipped: already running"

        self._running = True
        try:
            return await self._run_locked()
        finally:
            self._running = False

    async def _run_locked(self) -> str:
        config = self._context.config
        gateway = self._context.gcode_gateway
        if gateway is None:
            return "Code index nightly full reindex skipped: gcode gateway unavailable"

        projects = await self._context.run_db(self._context.storage.list_indexed_projects)
        concurrency = max(1, int(config.nightly_full_reindex_concurrency))
        semaphore = asyncio.Semaphore(concurrency)
        run_id = uuid4().hex

        async def reindex_project(project: Any) -> str:
            project_id = str(project.id)
            root_path = project.root_path
            try:
                if not root_path:
                    return f"{project_id}:skipped_missing_root"

                root = Path(str(root_path)).expanduser()
                if not await asyncio.to_thread(root.is_dir):
                    return f"{project_id}:skipped_missing_root"

                async with semaphore:
                    factory = getattr(self._context, "launch_factory", None)
                    timeout = config.nightly_full_reindex_timeout_seconds
                    if factory is None:
                        result = await gateway.nightly_full_reindex(root, timeout=timeout)
                    else:
                        with factory.open(project_id, timeout_seconds=timeout) as launch:
                            result = await gateway.nightly_full_reindex(
                                root, timeout=timeout, env=launch.env
                            )
                    if result.timed_out:
                        status = "timed_out"
                    elif result.success:
                        status = "completed"
                    else:
                        status = "failed"
                    log_gcode_maintenance_event(
                        log_file=config.maintenance_log_file,
                        event="nightly_full_reindex",
                        run_id=run_id,
                        project_id=project_id,
                        root_path=str(root),
                        result=result,
                        status=status,
                    )
                    return f"{project_id}:{status}"
            except Exception:
                logger.exception(
                    "Code index nightly full reindex failed for %s at %s",
                    project_id,
                    root_path,
                )
                return f"{project_id}:failed"

        if concurrency == 1:
            outcomes = [await reindex_project(project) for project in projects]
        else:
            outcomes = await asyncio.gather(*(reindex_project(project) for project in projects))
        completed = sum(1 for outcome in outcomes if outcome.endswith(":completed"))
        failed = sum(
            1
            for outcome in outcomes
            if outcome.endswith(":failed") or outcome.endswith(":timed_out")
        )
        skipped = len(outcomes) - completed - failed
        return (
            "Code index nightly full reindex completed: "
            f"run_id={run_id} completed={completed} failed={failed} skipped={skipped}"
        )


def create_code_index_nightly_reindex_handler(
    reindexer: CodeIndexNightlyFullReindexer,
) -> CronHandler:
    async def _handler(_job: CronJob) -> str:
        return await reindexer.run_once()

    return _handler


def register_code_index_nightly_reindex_cron(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    reindexer: CodeIndexNightlyFullReindexer,
    config: CodeIndexConfig,
    project_id: str | None,
) -> None:
    """Register the global nightly full reindex system cron job."""
    cron_executor.register_handler(
        CODE_INDEX_NIGHTLY_REINDEX_HANDLER,
        create_code_index_nightly_reindex_handler(reindexer),
    )
    timezone = resolve_local_timezone(config.nightly_full_reindex_timezone)
    enabled = bool(config.nightly_full_reindex_enabled)
    action_config = {
        "handler": CODE_INDEX_NIGHTLY_REINDEX_HANDLER,
        "purpose": CODE_INDEX_NIGHTLY_REINDEX_DESCRIPTION,
        "timeout_seconds": config.nightly_full_reindex_timeout_seconds,
        "concurrency": config.nightly_full_reindex_concurrency,
        "log_file": config.maintenance_log_file,
    }

    existing = cron_storage.get_job_by_name(CODE_INDEX_NIGHTLY_REINDEX_JOB_NAME)
    if existing is None:
        cron_storage.create_job(
            project_id=project_id or PERSONAL_PROJECT_ID,
            name=CODE_INDEX_NIGHTLY_REINDEX_JOB_NAME,
            description=CODE_INDEX_NIGHTLY_REINDEX_DESCRIPTION,
            schedule_type="cron",
            cron_expr=config.nightly_full_reindex_cron,
            timezone=timezone,
            action_type="handler",
            action_config=action_config,
            enabled=enabled,
            is_system=True,
        )
        return

    if not existing.is_system:
        cron_storage.mark_as_system_job(existing.id)

    repaired = cron_storage.reconcile_system_job_definition(
        existing.id,
        action_type="handler",
        action_config=action_config,
        description=CODE_INDEX_NIGHTLY_REINDEX_DESCRIPTION,
        schedule_type="cron",
        cron_expr=config.nightly_full_reindex_cron,
        interval_seconds=None,
        run_at=None,
        timezone=timezone,
    )
    if repaired is not None:
        _reconcile_enabled_state(cron_storage, repaired, enabled)


def _reconcile_enabled_state(
    cron_storage: CronJobStorage,
    job: CronJob,
    enabled: bool,
) -> None:
    if job.enabled == enabled:
        if enabled and job.next_run_at is None:
            cron_storage.wake_system_job(job.id)
        return

    if not enabled:
        cron_storage.reconcile_system_job_identity(job.id, enabled=False, next_run_at=None)
        return

    enabled_job = replace(job, enabled=True)
    next_run = compute_next_run(enabled_job)
    cron_storage.reconcile_system_job_identity(
        job.id,
        enabled=True,
        next_run_at=next_run.isoformat() if next_run else None,
    )
