"""gcode prune automation for code-index projection drift."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from gobby.scheduler.executor import CronHandler
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob

if TYPE_CHECKING:
    from gobby.code_index.context import CodeIndexContext

logger = logging.getLogger(__name__)

CODE_INDEX_PRUNE_JOB_NAME = "gobby:code-index-prune"
CODE_INDEX_PRUNE_HANDLER = "code-index:prune"
CODE_INDEX_PRUNE_INTERVAL_SECONDS = 3600
CODE_INDEX_PRUNE_DESCRIPTION = "Prune stale code-index graph and vector projections"


class CronRegistrationProtocol(Protocol):
    def register_handler(self, name: str, handler: CronHandler) -> None: ...


class CodeIndexPruner:
    """Coordinates startup and cron gcode prune runs."""

    def __init__(self, context: CodeIndexContext, *, max_concurrency: int = 1) -> None:
        self._context = context
        self._global_semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task[str]] = set()

    def schedule_startup_prunes(self) -> asyncio.Task[dict[str, int]]:
        """Schedule one non-blocking prune attempt per indexed project."""
        task = asyncio.create_task(self._schedule_startup_prunes(), name="code-index-startup-prune")
        return task

    async def _schedule_startup_prunes(self) -> dict[str, int]:
        projects = await self._context.run_db(self._context.storage.list_indexed_projects)
        scheduled = 0
        skipped = 0
        for project in projects:
            if not project.root_path:
                skipped += 1
                continue
            self._start_background_prune(
                project_id=str(project.id),
                root_path=str(project.root_path),
                dirty=False,
                reason="startup",
            )
            scheduled += 1
        return {"scheduled": scheduled, "skipped": skipped}

    async def prune_dirty_projects(self, *, limit: int = 100) -> str:
        dirty_projects = await self._context.run_db(
            self._context.storage.list_prune_dirty_projects,
            limit,
        )
        if not dirty_projects:
            return "Code index prune skipped: dirty=0"

        outcomes: list[str] = []
        for dirty in dirty_projects:
            outcomes.append(
                await self.prune_project(
                    project_id=dirty.project_id,
                    root_path=dirty.root_path,
                    dirty=True,
                    reason=dirty.reason,
                )
            )
        return "Code index prune completed: " + ", ".join(outcomes)

    async def prune_project(
        self,
        *,
        project_id: str,
        root_path: str,
        dirty: bool,
        reason: str,
    ) -> str:
        lock = self._project_locks.setdefault(project_id, asyncio.Lock())
        if lock.locked():
            return f"{project_id}:skipped_locked"

        async with lock:
            pending = await self._context.run_db(
                self._context.storage.get_pending_sync_files,
                project_id,
                1,
                vectors=True,
                graph=True,
            )
            if pending:
                return f"{project_id}:deferred_pending_sync"

            gateway = self._context.gcode_gateway
            if gateway is None:
                await self._record_failure_if_dirty(
                    project_id,
                    dirty,
                    "gcode gateway unavailable",
                )
                return f"{project_id}:failed"

            async with self._global_semaphore:
                try:
                    result = await gateway.prune(Path(root_path).expanduser())
                    if not result.get("success", True):
                        raise RuntimeError(result.get("error", "gcode prune failed"))
                except Exception as exc:
                    await self._record_failure_if_dirty(project_id, dirty, str(exc))
                    logger.warning(
                        "Code index prune failed for %s at %s: %s",
                        project_id,
                        root_path,
                        exc,
                        exc_info=True,
                    )
                    return f"{project_id}:failed"

            await self._context.run_db(self._context.storage.clear_prune_dirty, project_id)
            logger.debug("Code index prune completed for %s (%s)", project_id, reason)
            return f"{project_id}:pruned"

    def _start_background_prune(
        self,
        *,
        project_id: str,
        root_path: str,
        dirty: bool,
        reason: str,
    ) -> None:
        task = asyncio.create_task(
            self.prune_project(
                project_id=project_id,
                root_path=root_path,
                dirty=dirty,
                reason=reason,
            ),
            name=f"code-index-prune:{project_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _record_failure_if_dirty(self, project_id: str, dirty: bool, error: str) -> None:
        if dirty:
            await self._context.run_db(
                self._context.storage.record_prune_failure, project_id, error
            )


def create_code_index_prune_handler(pruner: CodeIndexPruner) -> CronHandler:
    async def _handler(job: CronJob) -> str:
        raw_limit = job.action_config.get("limit", 100)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 100
        return await pruner.prune_dirty_projects(limit=max(1, limit))

    return _handler


def register_code_index_prune_cron(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    pruner: CodeIndexPruner,
    project_id: str | None,
) -> None:
    """Register the global hourly code-index prune system cron job."""
    cron_executor.register_handler(
        CODE_INDEX_PRUNE_HANDLER, create_code_index_prune_handler(pruner)
    )
    action_config = {
        "handler": CODE_INDEX_PRUNE_HANDLER,
        "purpose": CODE_INDEX_PRUNE_DESCRIPTION,
        "limit": 100,
    }
    existing = cron_storage.get_job_by_name(CODE_INDEX_PRUNE_JOB_NAME)
    if existing is None:
        cron_storage.create_job(
            project_id=project_id or "system",
            name=CODE_INDEX_PRUNE_JOB_NAME,
            description=CODE_INDEX_PRUNE_DESCRIPTION,
            schedule_type="interval",
            interval_seconds=CODE_INDEX_PRUNE_INTERVAL_SECONDS,
            action_type="handler",
            action_config=action_config,
            enabled=True,
            is_system=True,
        )
        return

    if not existing.is_system:
        cron_storage.mark_as_system_job(existing.id)

    repaired = cron_storage.reconcile_system_job_definition(
        existing.id,
        action_type="handler",
        action_config=action_config,
        description=CODE_INDEX_PRUNE_DESCRIPTION,
        schedule_type="interval",
        interval_seconds=CODE_INDEX_PRUNE_INTERVAL_SECONDS,
    )
    if repaired is not None and not repaired.enabled:
        cron_storage.toggle_job(repaired.id)
    elif repaired is not None and repaired.next_run_at is None:
        cron_storage.wake_system_job(repaired.id)
