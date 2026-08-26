"""Cron scheduler - background task that checks for and dispatches due jobs."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, cast

from gobby.config.cron import CronConfig
from gobby.config.runtime import RuntimeActiveBundle
from gobby.scheduler.executor import CronExecutor
from gobby.storage.cron import CronJobStorage, compute_next_run, is_removed_automation_job
from gobby.storage.cron_models import CronJob, CronRun
from gobby.storage.hub.protocol import CronRunAdmission
from gobby.utils.datetime import utc_now
from gobby.utils.machine_id import require_machine_id
from gobby.utils.project_context import (
    get_project_context,
    reset_project_context,
    set_project_context,
    set_project_context_from_ref,
)
from gobby.utils.session_context import reset_session_context, set_session_context

logger = logging.getLogger(__name__)
CronRunRejectionCode = Literal["cron_job_already_running", "cron_max_concurrent_jobs"]


class CronRunRejected(RuntimeError):
    """Raised when a manual cron run is rejected by scheduler execution guards."""

    def __init__(self, code: CronRunRejectionCode, message: str):
        super().__init__(message)
        self.code = code


class _ScheduledRunNotAdmitted(RuntimeError):
    """Roll back a claimed schedule when run insertion loses admission."""


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
        capture_bundle: Callable[[], RuntimeActiveBundle],
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ):
        self.storage = storage
        self.executor = executor
        self._capture_bundle = capture_bundle
        self._running = False
        self._check_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._active_run_ids: set[str] = set()
        self._machine_id = require_machine_id()
        self._scheduler_owner = str(uuid.uuid4())
        self._run_db_callback = run_db
        self.on_run_complete: Callable[[CronJob, CronRun], Awaitable[None]] | None = None

    async def _run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run synchronous scheduler storage work outside the event loop."""
        if self._run_db_callback is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db_callback(func, *args, **kwargs)

    async def start(self) -> None:
        """Start the scheduler loops."""
        if self._running:
            return

        config = self._capture_config()

        await self._run_db(self._reconcile_interrupted_runs_on_startup)
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
            "Cron scheduler started (interval=%ss, max_concurrent=%s)",
            config.check_interval_seconds,
            config.max_concurrent_jobs,
        )

    def _capture_config(self) -> CronConfig:
        return self._capture_bundle().snapshot.active.cron

    def _reconcile_interrupted_runs_on_startup(self) -> None:
        result = self.storage.reconcile_interrupted_runs(self._machine_id)
        if result["dispatched"] or result["interrupted"]:
            logger.info(
                "Reconciled cron runs at scheduler startup: "
                "dispatched=%s interrupted=%s requeued=%s",
                result["dispatched"],
                result["interrupted"],
                result["requeued"],
            )

    def list_protected_runs(self) -> list[dict[str, Any]]:
        """Report this daemon's active restart-protected cron runs.

        The running ``cron_runs`` row is the restart lease: it exists from run
        start until any terminal status, and startup reconciliation closes the
        rows a dead daemon left behind. A run past its own action timeout no
        longer holds the lease — the executor is already failing it.
        """
        now = utc_now()
        protected: list[dict[str, Any]] = []
        for run in self.storage.list_active_runs(scheduler_owner=self._scheduler_owner):
            job = self.storage.get_job(run.cron_job_id)
            if job is None or not job.restart_protected:
                continue
            started_at = run.started_at or run.triggered_at
            elapsed = max(0.0, (now - started_at).total_seconds())
            remaining = self.executor.action_timeout_seconds(job) - elapsed
            if remaining <= 0:
                continue
            protected.append(
                {
                    "run_id": run.id,
                    "job_id": job.id,
                    "job_name": job.name,
                    "started_at": started_at.isoformat(),
                    "elapsed_seconds": elapsed,
                    "remaining_seconds": remaining,
                }
            )
        return protected

    def _track_run_task(self, task: asyncio.Task[None], run_id: str) -> None:
        """Track an in-flight execution task and the cron run row it owns."""
        self._active_tasks.add(task)
        self._active_run_ids.add(run_id)

        def _untrack(done: asyncio.Task[None]) -> None:
            self._active_tasks.discard(done)
            self._active_run_ids.discard(run_id)

        task.add_done_callback(_untrack)

    def _sweep_orphaned_active_runs(self) -> int:
        """Fail active runs that no live task in this process is executing.

        The scheduler is the only writer of pending/running cron_runs rows, so
        an active row without a tracked task (cancelled execution, terminal
        update lost to a DB error) can never complete and would wedge its job
        and a concurrency slot until daemon restart. Liveness — not age — is
        the discriminator: long-running handlers stay tracked and are never
        swept, no matter how old their run is.
        """
        swept = 0
        for run in self.storage.list_active_runs(scheduler_owner=self._scheduler_owner):
            if run.id in self._active_run_ids:
                continue
            if self.storage.fail_run_if_active(
                run.id,
                error="Orphaned cron run had no live scheduler task at dispatch time",
            ):
                logger.warning(
                    "Failed orphaned cron run %s (job %s): no live scheduler task",
                    run.id,
                    run.cron_job_id,
                )
                swept += 1
        return swept

    def _sweep_stale_running_runs(self, config: CronConfig) -> int:
        """Fail timed-out runs so they stop consuming scheduler capacity."""
        swept = self.storage.fail_stale_running_runs(
            config.stale_run_timeout_seconds,
            machine_id=self._machine_id,
            exclude_run_ids=self._active_run_ids,
        )
        if swept:
            logger.warning("Marked %s stale cron run(s) failed before dispatch", swept)
        return swept

    def _create_scheduled_run(self, job: CronJob, config: CronConfig) -> CronRun | None:
        """Claim a due schedule and create its run in one database transaction."""
        if job.next_run_at is None:
            return None
        next_run = compute_next_run(job)
        try:
            with self.storage.db.transaction_immediate(lock=CronRunAdmission()):
                if self.storage.count_running(self._machine_id) >= config.max_concurrent_jobs:
                    return None
                claimed = self.storage.claim_due_job(
                    job.id,
                    expected_next_run_at=job.next_run_at,
                    next_run_at=next_run,
                    disable=job.schedule_type == "once" and next_run is None,
                )
                if not claimed:
                    return None
                run = self.storage.create_run(
                    job.id,
                    scheduler_owner=self._scheduler_owner,
                    start_immediately=True,
                )
                if run is None:
                    raise _ScheduledRunNotAdmitted
            return run
        except _ScheduledRunNotAdmitted:
            return None

    def _resolve_project_context(self, project_id: str) -> dict[str, Any]:
        """Resolve an enriched project context without leaking a worker token."""
        token = set_project_context_from_ref(project_id, self.storage.db)
        if token is None:
            return {"id": project_id}
        try:
            return get_project_context() or {"id": project_id}
        finally:
            reset_project_context(token)

    async def stop(self) -> None:
        """Stop the scheduler loops gracefully."""
        self._running = False
        tasks = [t for t in [self._check_task, self._cleanup_task] if t]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        active_tasks = list(self._active_tasks)
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        await self.executor.shutdown()
        logger.info("Cron scheduler stopped")

    async def _check_loop(self) -> None:
        """Poll for due jobs and dispatch them."""
        while self._running:
            config = self._capture_config()
            try:
                if config.enabled:
                    await self._check_due_jobs(config)
            except Exception as e:
                logger.exception("Cron check loop error: %s", e)
            try:
                await asyncio.sleep(config.check_interval_seconds)
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
            config = self._capture_config()
            try:
                deleted = 0
                if config.enabled:
                    deleted = await self._run_db(
                        self.storage.cleanup_old_runs,
                        config.cleanup_after_days,
                    )
                if deleted > 0:
                    logger.info("Cleaned up %s old cron runs", deleted)
            except Exception as e:
                logger.exception("Cron cleanup error: %s", e)

    async def _check_due_jobs(self, config: CronConfig) -> None:
        """Check for due jobs and dispatch them."""
        removed = await self._run_db(self.storage.delete_removed_automation_jobs)
        if removed:
            logger.info("Deleted %s removed automation cron job(s)", removed)

        await self._run_db(self._sweep_stale_running_runs, config)
        await self._run_db(self._sweep_orphaned_active_runs)

        due_jobs = await self._run_db(self.storage.get_due_jobs)
        if not due_jobs:
            return

        # Respect max concurrent limit
        running_count = await self._run_db(
            self.storage.count_running,
            self._machine_id,
        )
        available_slots = config.max_concurrent_jobs - running_count

        if available_slots <= 0:
            logger.debug(
                "Skipping %s due jobs: %s/%s slots used",
                len(due_jobs),
                running_count,
                config.max_concurrent_jobs,
            )
            return

        dispatched = 0
        for job in due_jobs:
            if dispatched >= available_slots:
                break
            try:
                if is_removed_automation_job(job):
                    logger.info(
                        "Skipping removed automation cron job %s (%s)",
                        job.id,
                        job.name,
                    )
                    continue

                # Check backoff for consecutive failures
                if job.consecutive_failures > 0:
                    backoff = self._get_backoff_seconds(job.consecutive_failures, config)
                    if job.last_run_at:
                        elapsed = (datetime.now(UTC) - job.last_run_at).total_seconds()
                        if elapsed < backoff:
                            logger.debug(
                                "Skipping job %s (%s): backoff %ss, elapsed %.0fs",
                                job.id,
                                job.name,
                                backoff,
                                elapsed,
                            )
                            continue

                # The schedule CAS is global: whichever machine wins dispatches the occurrence.
                run = await self._run_db(self._create_scheduled_run, job, config)
                if run is None:
                    logger.debug(
                        "Skipping cron job %s (%s): previous run still active",
                        job.id,
                        job.name,
                    )
                    continue
                logger.debug(
                    "Dispatching cron job %s (%s), run %s",
                    job.id,
                    job.name,
                    run.id,
                )

                # Track background task to prevent GC and await on stop
                task = asyncio.create_task(
                    self._execute_and_update(job, run, config),
                    name=f"cron-run-{run.id}",
                    context=contextvars.Context(),
                )
                self._track_run_task(task, run.id)
                dispatched += 1
            except Exception as e:
                logger.exception("Failed to dispatch cron job %s: %s", job.id, e)

    async def _execute_and_update(
        self,
        job: CronJob,
        run: CronRun | None,
        config: CronConfig,
    ) -> None:
        """Execute a job and update its status afterward."""
        if not run:
            logger.error("Cannot execute job %s: valid run record required", job.id)
            return
        session_token = set_session_context(None)
        project_token = None
        try:
            if job.project_id:
                project_ctx = await self._run_db(self._resolve_project_context, job.project_id)
                project_token = set_project_context(project_ctx)
            result: CronRun | None = None
            try:
                result = await self.executor.execute(job, run)

                # Update job status
                now = datetime.now(UTC).isoformat()
                if result.status == "failed":
                    # Increment failure counter (next_run_at already set before dispatch)
                    failures = job.consecutive_failures + 1
                    backoff = self._get_backoff_seconds(failures, config)
                    await self._run_db(
                        self._update_job_bookkeeping,
                        job,
                        last_run_at=now,
                        last_status=result.status,
                        consecutive_failures=failures,
                    )
                    logger.warning(
                        "Cron job %s (%s) failed; applying %ss backoff after "
                        "%s consecutive failure%s",
                        job.id,
                        job.name,
                        backoff,
                        failures,
                        "" if failures == 1 else "s",
                    )
                else:
                    # Reset failure counter (next_run_at already set before dispatch)
                    await self._run_db(
                        self._update_job_bookkeeping,
                        job,
                        last_run_at=now,
                        last_status=result.status,
                        consecutive_failures=0,
                    )

            except Exception as e:
                now = datetime.now(UTC).isoformat()
                failures = job.consecutive_failures + 1
                backoff = self._get_backoff_seconds(failures, config)
                logger.exception(
                    "Unexpected error executing cron job %s: %s; applying %ss backoff after "
                    "%s consecutive failure%s",
                    job.id,
                    e,
                    backoff,
                    failures,
                    "" if failures == 1 else "s",
                )
                await self._run_db(
                    self._update_job_bookkeeping,
                    job,
                    last_run_at=now,
                    last_status="failed",
                    consecutive_failures=failures,
                )
                # The executor normally terminalizes the run row; if it raised
                # instead, fail the row so it cannot wedge the job's dispatch.
                try:
                    await self._run_db(
                        self.storage.fail_run_if_active,
                        run.id,
                        error=f"Scheduler failed to finalize run: {e}",
                    )
                except Exception:
                    logger.debug("Failed to finalize errored cron run %s", run.id, exc_info=True)
                result = await self._run_db(self.storage.get_run, run.id)

            # Fire event callback (best-effort, non-blocking)
            if self.on_run_complete and result:
                try:
                    await self.on_run_complete(job, result)
                except Exception as exc:
                    logger.debug("Cron event callback failed: %s", exc)
        finally:
            if project_token is not None:
                reset_project_context(project_token)
            reset_session_context(session_token)

    @staticmethod
    def _get_backoff_seconds(consecutive_failures: int, config: CronConfig) -> int:
        """Get backoff delay based on number of consecutive failures."""
        delays = config.backoff_delays
        if not delays:
            return 0
        idx = min(consecutive_failures - 1, len(delays) - 1)
        return delays[idx]

    def _update_job_bookkeeping(self, job: CronJob, **fields: object) -> CronJob | None:
        if job.is_system:
            return self.storage.update_system_job_bookkeeping(job.id, **fields)
        return self.storage.update_job(job.id, **fields)

    async def run_now(self, job_id: str) -> CronRun | None:
        """Trigger immediate execution of a job (bypasses schedule).

        The job executes in the background via create_task. The returned
        CronRun will initially have status 'pending'; poll the run or
        check job.last_status for the final result.
        """
        job = await self._run_db(self.storage.get_job, job_id)
        if not job:
            return None

        config = self._capture_config()
        await self._run_db(self._sweep_stale_running_runs, config)
        await self._run_db(self._sweep_orphaned_active_runs)

        run, running_count, already_running = cast(
            tuple[CronRun | None, int, bool],
            await self._run_db(
                self.storage.create_run_if_admitted,
                job.id,
                machine_id=self._machine_id,
                max_concurrent_jobs=config.max_concurrent_jobs,
                scheduler_owner=self._scheduler_owner,
            ),
        )
        if run is None and already_running:
            return None
        if run is None and running_count >= config.max_concurrent_jobs:
            raise CronRunRejected(
                "cron_max_concurrent_jobs",
                "Cron scheduler is at max concurrency "
                f"({running_count}/{config.max_concurrent_jobs})",
            )
        if run is None:
            return None
        logger.info("Manual trigger: cron job %s (%s), run %s", job.id, job.name, run.id)

        # Execute in background
        task = asyncio.create_task(
            self._execute_and_update(job, run, config),
            name=f"cron-run-manual-{run.id}",
            context=contextvars.Context(),
        )
        self._track_run_task(task, run.id)

        return run
