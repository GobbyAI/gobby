"""Cron scheduler - background task that checks for and dispatches due jobs."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from gobby.config.cron import CronConfig
from gobby.scheduler.executor import CronExecutor
from gobby.shutdown_intent import ShutdownIntent, read_active_shutdown_intent
from gobby.storage.cron import CronJobStorage, compute_next_run
from gobby.storage.cron_models import CronJob, CronRun
from gobby.utils.project_context import (
    reset_project_context,
    set_project_context,
    set_project_context_from_ref,
)
from gobby.utils.session_context import reset_session_context, set_session_context

logger = logging.getLogger(__name__)
PLANNED_RESTART_MARKER_MAX_AGE_SECONDS = 120.0


class CronScheduler:
    """Background scheduler that polls for due cron jobs and dispatches them.

    Follows the SessionLifecycleManager dual-loop pattern:
    - _check_loop: polls for due jobs every check_interval_seconds
    - _cleanup_loop: deletes old run history every 6 hours
    """

    def __init__(
        self,
        storage: CronJobStorage,
        executor: CronExecutor,
        config: CronConfig,
    ):
        self.storage = storage
        self.executor = executor
        self.config = config
        self._running = False
        self._check_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._active_tasks: set[asyncio.Task[None]] = set()
        self.on_run_complete: Callable[[CronJob, CronRun], Awaitable[None]] | None = None

    async def start(self) -> None:
        """Start the scheduler loops."""
        if self._running:
            return
        if not self.config.enabled:
            logger.info("Cron scheduler disabled by config")
            return

        self._running = True
        self._check_task = asyncio.create_task(
            self._check_loop(),
            name="cron-scheduler-check",
        )
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="cron-scheduler-cleanup",
        )
        logger.info(
            f"Cron scheduler started (interval={self.config.check_interval_seconds}s, "
            f"max_concurrent={self.config.max_concurrent_jobs})"
        )

    async def stop(self) -> None:
        """Stop the scheduler loops gracefully."""
        self._running = False
        tasks = [t for t in [self._check_task, self._cleanup_task] if t]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Wait for in-flight job executions to finish
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        logger.info("Cron scheduler stopped")

    async def _check_loop(self) -> None:
        """Poll for due jobs and dispatch them."""
        while self._running:
            try:
                await self._check_due_jobs()
            except Exception as e:
                logger.error(f"Cron check loop error: {e}", exc_info=True)
            try:
                await asyncio.sleep(self.config.check_interval_seconds)
            except asyncio.CancelledError:
                break

    async def _cleanup_loop(self) -> None:
        """Periodically clean up old run history."""
        cleanup_interval = 6 * 3600  # 6 hours
        while self._running:
            try:
                await asyncio.sleep(cleanup_interval)
            except asyncio.CancelledError:
                break
            try:
                deleted = self.storage.cleanup_old_runs(self.config.cleanup_after_days)
                if deleted > 0:
                    logger.info(f"Cleaned up {deleted} old cron runs")
            except Exception as e:
                logger.error(f"Cron cleanup error: {e}", exc_info=True)

    async def _check_due_jobs(self) -> None:
        """Check for due jobs and dispatch them."""
        due_jobs = self.storage.get_due_jobs()
        if not due_jobs:
            return

        expired_runs = self.storage.fail_stale_running_runs(self.config.running_timeout_seconds)
        if expired_runs:
            restart_source = self._planned_restart_source()
            if restart_source:
                logger.info(
                    "Marked %s stale cron run(s) failed during planned restart before dispatch "
                    "(source=%s)",
                    expired_runs,
                    restart_source,
                )
            else:
                logger.warning("Marked %s stale cron run(s) failed before dispatch", expired_runs)

        # Respect max concurrent limit
        running_count = self.storage.count_running()
        available_slots = self.config.max_concurrent_jobs - running_count

        if available_slots <= 0:
            logger.debug(
                f"Skipping {len(due_jobs)} due jobs: "
                f"{running_count}/{self.config.max_concurrent_jobs} slots used"
            )
            return

        dispatched = 0
        for job in due_jobs:
            if dispatched >= available_slots:
                break
            try:
                if self.storage.has_running_run(job.id):
                    logger.debug(
                        "Skipping cron job %s (%s): previous run still active",
                        job.id,
                        job.name,
                    )
                    continue

                # Check backoff for consecutive failures
                if job.consecutive_failures > 0:
                    backoff = self._get_backoff_seconds(job.consecutive_failures)
                    if job.last_run_at:
                        last = datetime.fromisoformat(job.last_run_at)
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=UTC)
                        elapsed = (datetime.now(UTC) - last).total_seconds()
                        if elapsed < backoff:
                            logger.debug(
                                f"Skipping job {job.id} ({job.name}): "
                                f"backoff {backoff}s, elapsed {elapsed:.0f}s"
                            )
                            continue

                # Create run and advance next_run_at immediately to prevent re-dispatch
                run = self.storage.create_run(job.id)
                next_run = compute_next_run(job)
                self._update_job_bookkeeping(
                    job,
                    next_run_at=next_run.isoformat() if next_run else None,
                )
                logger.info(f"Dispatching cron job {job.id} ({job.name}), run {run.id}")

                # Track background task to prevent GC and await on stop
                task = asyncio.create_task(
                    self._execute_and_update(job, run),
                    name=f"cron-run-{run.id}",
                    context=contextvars.Context(),
                )
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)
                dispatched += 1
            except Exception as e:
                logger.error(f"Failed to dispatch cron job {job.id}: {e}", exc_info=True)

    async def _execute_and_update(self, job: CronJob, run: CronRun | None) -> None:
        """Execute a job and update its status afterward."""
        if not run:
            logger.error(f"Cannot execute job {job.id}: valid run record required")
            return
        session_token = set_session_context(None)
        project_token = None
        if job.project_id:
            project_token = set_project_context_from_ref(job.project_id, self.storage.db)
            if project_token is None:
                project_token = set_project_context({"id": job.project_id})
        try:
            result: CronRun | None = None
            try:
                result = await self.executor.execute(job, run)

                # Update job status
                now = datetime.now(UTC).isoformat()
                if result.status == "completed":
                    # Reset failure counter (next_run_at already set before dispatch)
                    self._update_job_bookkeeping(
                        job,
                        last_run_at=now,
                        last_status="completed",
                        consecutive_failures=0,
                    )
                else:
                    # Increment failure counter (next_run_at already set before dispatch)
                    failures = job.consecutive_failures + 1
                    self._update_job_bookkeeping(
                        job,
                        last_run_at=now,
                        last_status="failed",
                        consecutive_failures=failures,
                    )
                    logger.warning(
                        f"Cron job {job.id} ({job.name}) failed ({failures} consecutive failures)"
                    )

            except Exception as e:
                logger.error(f"Unexpected error executing cron job {job.id}: {e}", exc_info=True)

            # Fire event callback (best-effort, non-blocking)
            if self.on_run_complete and result:
                try:
                    await self.on_run_complete(job, result)
                except Exception as exc:
                    logger.debug(f"Cron event callback failed: {exc}")
        finally:
            if project_token is not None:
                reset_project_context(project_token)
            reset_session_context(session_token)

    def _get_backoff_seconds(self, consecutive_failures: int) -> int:
        """Get backoff delay based on number of consecutive failures."""
        delays = self.config.backoff_delays
        if not delays:
            return 0
        idx = min(consecutive_failures - 1, len(delays) - 1)
        return delays[idx]

    def _planned_restart_source(self) -> str | None:
        record = read_active_shutdown_intent(max_age_seconds=PLANNED_RESTART_MARKER_MAX_AGE_SECONDS)
        if record is None or record.stale or record.error:
            return None
        if record.intent is not ShutdownIntent.RESTART:
            return None
        return record.source

    def _update_job_bookkeeping(self, job: CronJob, **fields: object) -> CronJob | None:
        if job.is_system:
            return self.storage.update_system_job_bookkeeping(job.id, **fields)
        return self.storage.update_job(job.id, **fields)

    async def run_now(self, job_id: str) -> CronRun | None:
        """Trigger immediate execution of a job (bypasses schedule).

        The job executes in the background via create_task. The returned
        CronRun will initially have status 'running'; poll the run or
        check job.last_status for the final result.
        """
        job = self.storage.get_job(job_id)
        if not job:
            return None

        run = self.storage.create_run(job.id)
        logger.info(f"Manual trigger: cron job {job.id} ({job.name}), run {run.id}")

        # Execute in background
        task = asyncio.create_task(
            self._execute_and_update(job, run),
            name=f"cron-run-manual-{run.id}",
            context=contextvars.Context(),
        )
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

        return run
