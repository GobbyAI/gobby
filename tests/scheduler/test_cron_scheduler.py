"""Tests for CronScheduler background task logic."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.cron import CronConfig
from gobby.scheduler.executor import CronExecutor
from gobby.scheduler.scheduler import CronRunRejected, CronScheduler
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_children import (
    INTERRUPTED_RUN_ERROR,
    INTERRUPTED_RUN_RETRY_DELAY_SECONDS,
)
from gobby.storage.cron_models import CronJob, CronRun
from tests._timing import drain_asyncio_tasks, wait_for_async_condition
from tests.config_runtime_helpers import static_cron_capture

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

PROJECT_ID = "00000000-0000-0000-0000-000000000000"

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def cron_storage(temp_db: HubDatabase) -> CronJobStorage:
    return CronJobStorage(temp_db)


@pytest.fixture
def mock_executor(cron_storage: CronJobStorage) -> CronExecutor:
    executor = CronExecutor(storage=cron_storage)

    def _complete_run(job: Any, run: CronRun) -> CronRun:
        """Helper to mark a run as completed."""
        now = datetime.now(UTC).isoformat()
        updated = cron_storage.update_run(run.id, status="completed", completed_at=now)
        return updated or run

    executor.execute = AsyncMock(side_effect=_complete_run)
    return executor


@pytest.fixture
def config() -> CronConfig:
    return CronConfig(check_interval_seconds=60, max_concurrent_jobs=5)


@pytest.fixture
def scheduler(
    cron_storage: CronJobStorage, mock_executor: CronExecutor, config: CronConfig
) -> CronScheduler:
    return CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )


@pytest.mark.asyncio
async def test_scheduler_advances_dispatcher_next_run_at(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    columns = {
        row["column_name"]
        for row in cron_storage.db.fetchall(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            ("cron_jobs",),
        )
    }
    if "is_system" not in columns:
        cron_storage.db.execute(
            "ALTER TABLE cron_jobs ADD COLUMN is_system BOOLEAN NOT NULL DEFAULT FALSE"
        )
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="gobby:test-dispatcher",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "dispatch.tick"},
        interval_seconds=60,
    )
    cron_storage.db.execute("UPDATE cron_jobs SET is_system = TRUE WHERE id = %s", (job.id,))
    cron_storage.update_system_job_bookkeeping(
        job.id,
        next_run_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )

    await scheduler._check_due_jobs(scheduler._capture_config())
    await wait_for_async_condition(
        lambda: (
            (updated := cron_storage.get_job(job.id)) is not None
            and updated.next_run_at is not None
            and updated.next_run_at != job.next_run_at
        ),
        description="dispatcher next_run_at update",
    )

    updated = cron_storage.get_job(job.id)
    assert updated is not None
    assert updated.next_run_at is not None
    assert updated.next_run_at != job.next_run_at


@pytest.mark.asyncio
async def test_start_creates_tasks(scheduler: CronScheduler) -> None:
    """start() creates check and cleanup tasks."""
    await scheduler.start()
    assert scheduler._running is True
    assert scheduler._check_task is not None
    assert scheduler._cleanup_task is not None
    await scheduler.stop()


@pytest.mark.asyncio
async def test_start_interrupts_orphan_running_runs_before_first_tick(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
) -> None:
    """Rows left running by a previous daemon must not suppress the first tick."""
    config = CronConfig(check_interval_seconds=60, max_concurrent_jobs=1)
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="gobby:test-dispatcher",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "dispatch.tick"},
        interval_seconds=60,
    )
    stale_run = cron_storage.create_run(job.id)
    cron_storage.update_run(stale_run.id, status="running")
    cron_storage.update_job(
        job.id, next_run_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    )

    try:
        await scheduler.start()
        await wait_for_async_condition(
            lambda: mock_executor.execute.await_count >= 1,
            description="dispatch after startup stale cron cleanup",
        )
    finally:
        await scheduler.stop()

    refreshed_run = cron_storage.get_run(stale_run.id)
    assert refreshed_run is not None
    assert refreshed_run.status == "interrupted"
    assert refreshed_run.error == INTERRUPTED_RUN_ERROR
    mock_executor.execute.assert_called_once()
    assert mock_executor.execute.await_args.args[0].id == job.id


@pytest.mark.asyncio
async def test_start_requeues_interrupted_job_without_charging_backoff(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
) -> None:
    """A run left running by a dead daemon closes as interrupted, not failed (#21021).

    The job keeps its failure counter and is pulled forward to a near-term
    retry instead of waiting for its next schedule slot.
    """
    scheduler = CronScheduler(
        storage=cron_storage,
        executor=mock_executor,
        capture_bundle=static_cron_capture(
            CronConfig(check_interval_seconds=60, max_concurrent_jobs=1)
        ),
    )
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="gobby:test-dream",
        schedule_type="cron",
        action_type="handler",
        action_config={"handler": "memory.dream"},
        cron_expr="0 2 * * *",
    )
    next_slot = datetime.now(UTC) + timedelta(days=1)
    cron_storage.update_job(job.id, next_run_at=next_slot.isoformat(), consecutive_failures=2)
    orphan = cron_storage.create_run(job.id, start_immediately=True)
    assert orphan is not None

    before = datetime.now(UTC)
    try:
        await scheduler.start()
    finally:
        await scheduler.stop()

    refreshed_run = cron_storage.get_run(orphan.id)
    assert refreshed_run is not None
    assert refreshed_run.status == "interrupted"
    assert refreshed_run.error == INTERRUPTED_RUN_ERROR
    refreshed_job = cron_storage.get_job(job.id)
    assert refreshed_job is not None
    assert refreshed_job.consecutive_failures == 2
    assert refreshed_job.next_run_at is not None
    assert before <= refreshed_job.next_run_at
    assert refreshed_job.next_run_at <= before + timedelta(
        seconds=INTERRUPTED_RUN_RETRY_DELAY_SECONDS + 5
    )
    assert refreshed_job.next_run_at < next_slot
    cast(AsyncMock, mock_executor.execute).assert_not_called()


@pytest.mark.asyncio
async def test_start_interrupts_orphan_pending_runs_without_replay(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
) -> None:
    """Rows left pending by an old manual trigger close as interrupted, never replay."""
    scheduler = CronScheduler(
        storage=cron_storage,
        executor=mock_executor,
        capture_bundle=static_cron_capture(
            CronConfig(check_interval_seconds=60, max_concurrent_jobs=1)
        ),
    )
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Manual Pending",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "dispatch.tick"},
        interval_seconds=60,
    )
    stale_run = cron_storage.create_run(job.id)

    try:
        await scheduler.start()
    finally:
        await scheduler.stop()

    refreshed_run = cron_storage.get_run(stale_run.id)
    assert refreshed_run is not None
    assert refreshed_run.status == "interrupted"
    assert refreshed_run.error == INTERRUPTED_RUN_ERROR
    mock_executor.execute.assert_not_called()


def _protected_job(
    cron_storage: CronJobStorage,
    name: str = "gobby:test-dream",
    *,
    protected: bool = True,
    timeout_seconds: float = 3600.0,
) -> CronJob:
    action_config: dict[str, Any] = {"handler": "memory.dream", "timeout_seconds": timeout_seconds}
    if protected:
        action_config["restart_protected"] = True
    return cron_storage.create_job(
        project_id=PROJECT_ID,
        name=name,
        schedule_type="cron",
        action_type="handler",
        action_config=action_config,
        cron_expr="0 2 * * *",
    )


def test_list_protected_runs_reports_only_this_daemons_protected_leases(
    scheduler: CronScheduler,
    cron_storage: CronJobStorage,
) -> None:
    protected = _protected_job(cron_storage)
    unprotected = _protected_job(cron_storage, "gobby:test-prune", protected=False)
    foreign = _protected_job(cron_storage, "gobby:test-foreign")
    run = cron_storage.create_run(
        protected.id, scheduler_owner=scheduler._scheduler_owner, start_immediately=True
    )
    assert run is not None
    assert (
        cron_storage.create_run(
            unprotected.id, scheduler_owner=scheduler._scheduler_owner, start_immediately=True
        )
        is not None
    )
    assert (
        cron_storage.create_run(foreign.id, scheduler_owner="other-daemon", start_immediately=True)
        is not None
    )

    reported = scheduler.list_protected_runs()

    assert [entry["run_id"] for entry in reported] == [run.id]
    entry = reported[0]
    assert entry["job_id"] == protected.id
    assert entry["job_name"] == "gobby:test-dream"
    assert 0 <= entry["elapsed_seconds"] < 5
    assert 3595 < entry["remaining_seconds"] <= 3600


def test_list_protected_runs_expires_with_the_action_timeout(
    scheduler: CronScheduler,
    cron_storage: CronJobStorage,
) -> None:
    """A run older than its own timeout is the executor's to fail, not a lease."""
    job = _protected_job(cron_storage, timeout_seconds=3600.0)
    run = cron_storage.create_run(
        job.id, scheduler_owner=scheduler._scheduler_owner, start_immediately=True
    )
    assert run is not None
    cron_storage.update_run(run.id, started_at=datetime.now(UTC) - timedelta(hours=2))

    assert scheduler.list_protected_runs() == []


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "interrupted"])
def test_protected_run_lease_is_released_on_terminal_status(
    scheduler: CronScheduler,
    cron_storage: CronJobStorage,
    terminal_status: str,
) -> None:
    job = _protected_job(cron_storage)
    run = cron_storage.create_run(
        job.id, scheduler_owner=scheduler._scheduler_owner, start_immediately=True
    )
    assert run is not None
    assert [entry["run_id"] for entry in scheduler.list_protected_runs()] == [run.id]

    cron_storage.update_run(run.id, status=terminal_status, completed_at=datetime.now(UTC))

    assert scheduler.list_protected_runs() == []


@pytest.mark.asyncio
async def test_stop_cancels_tasks(scheduler: CronScheduler) -> None:
    """stop() cancels tasks gracefully."""
    await scheduler.start()
    await scheduler.stop()
    assert scheduler._running is False


@pytest.mark.asyncio
async def test_stop_shuts_down_executor(
    scheduler: CronScheduler, mock_executor: CronExecutor
) -> None:
    mock_executor.shutdown = AsyncMock()

    await scheduler.start()
    await scheduler.stop()

    mock_executor.shutdown.assert_awaited_once()
    assert scheduler._running is False
    assert scheduler._check_task is not None and scheduler._check_task.cancelled()
    assert scheduler._cleanup_task is not None and scheduler._cleanup_task.cancelled()


@pytest.mark.asyncio
async def test_stop_cancels_active_run_tasks(scheduler: CronScheduler) -> None:
    active_task = asyncio.create_task(asyncio.Event().wait())
    scheduler._active_tasks.add(active_task)

    await asyncio.wait_for(scheduler.stop(), timeout=0.5)

    assert active_task.cancelled()


@pytest.mark.asyncio
async def test_double_start_is_noop(scheduler: CronScheduler) -> None:
    """Calling start() twice doesn't create duplicate tasks."""
    await scheduler.start()
    task1 = scheduler._check_task
    await scheduler.start()  # Should be a no-op
    assert scheduler._check_task is task1
    await scheduler.stop()


@pytest.mark.asyncio
async def test_disabled_scheduler_starts_runtime_watch_loops() -> None:
    """A disabled scheduler stays alive so a live config swap can enable it."""
    config = CronConfig(enabled=False)
    executor = MagicMock()
    executor.shutdown = AsyncMock()
    scheduler = CronScheduler(
        storage=MagicMock(), executor=executor, capture_bundle=static_cron_capture(config)
    )
    await scheduler.start()
    assert scheduler._running is True
    assert scheduler._check_task is not None
    await scheduler.stop()


@pytest.mark.asyncio
async def test_check_due_jobs_dispatches(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """_check_due_jobs dispatches due jobs to executor."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )

    # Create a job with next_run in the past
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Due Job",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    cron_storage.update_job(job.id, next_run_at=past)

    await scheduler._check_due_jobs(scheduler._capture_config())
    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 1,
        description="cron execution dispatch",
    )

    mock_executor.execute.assert_called_once()
    assert mock_executor.execute.call_count == 1
    assert mock_executor.execute.call_args is not None


@pytest.mark.asyncio
async def test_check_due_jobs_dispatch_log_is_debug(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Due Job",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    cron_storage.update_job(job.id, next_run_at=past)

    with caplog.at_level("DEBUG", logger="gobby.scheduler.scheduler"):
        await scheduler._check_due_jobs(scheduler._capture_config())
    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 1,
        description="cron execution dispatch",
    )

    dispatch_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("Dispatching cron job")
    ]
    assert len(dispatch_records) == 1
    assert dispatch_records[0].levelname == "DEBUG"


@pytest.mark.asyncio
async def test_concurrent_schedulers_claim_due_job_once(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two schedulers selecting the same due row dispatch one running run."""
    second_storage = CronJobStorage(cron_storage.db)
    first_scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    second_scheduler = CronScheduler(
        storage=second_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Concurrent claim",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "test"},
        interval_seconds=60,
    )
    cron_storage.update_job(job.id, next_run_at=datetime.now(UTC) - timedelta(minutes=1))

    selection_barrier = threading.Barrier(2)
    first_get_due_jobs = cron_storage.get_due_jobs
    second_get_due_jobs = second_storage.get_due_jobs

    def first_synchronized_selection() -> list[CronJob]:
        jobs = first_get_due_jobs()
        selection_barrier.wait(timeout=2)
        return jobs

    def second_synchronized_selection() -> list[CronJob]:
        jobs = second_get_due_jobs()
        selection_barrier.wait(timeout=2)
        return jobs

    monkeypatch.setattr(cron_storage, "get_due_jobs", first_synchronized_selection)
    monkeypatch.setattr(second_storage, "get_due_jobs", second_synchronized_selection)

    release_execution = asyncio.Event()

    async def hold_running_run(_job: CronJob, run: CronRun) -> CronRun:
        await release_execution.wait()
        updated = cron_storage.update_run(
            run.id,
            status="completed",
            completed_at=datetime.now(UTC),
        )
        return updated or run

    mock_executor.execute.side_effect = hold_running_run

    try:
        await asyncio.gather(
            first_scheduler._check_due_jobs(first_scheduler._capture_config()),
            second_scheduler._check_due_jobs(second_scheduler._capture_config()),
        )
        await wait_for_async_condition(
            lambda: mock_executor.execute.await_count == 1,
            description="single claimed cron execution",
        )

        runs = cron_storage.list_runs(job.id, limit=10)
        assert len(runs) == 1
        assert runs[0].status == "running"
        mock_executor.execute.assert_awaited_once()
    finally:
        release_execution.set()
        await asyncio.gather(
            *first_scheduler._active_tasks,
            *second_scheduler._active_tasks,
        )


@pytest.mark.asyncio
async def test_check_due_jobs_keeps_loop_responsive_during_db_latency(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked heartbeat query runs on a worker while the loop keeps ticking."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    loop = asyncio.get_running_loop()
    started: asyncio.Future[None] = loop.create_future()
    release = threading.Event()
    blocked_at: list[float] = []

    def slow_cleanup() -> int:
        blocked_at.append(time.monotonic())
        loop.call_soon_threadsafe(started.set_result, None)
        failsafe = threading.Timer(0.5, release.set)
        failsafe.start()
        try:
            release.wait()
        finally:
            failsafe.cancel()
        return 0

    monkeypatch.setattr(cron_storage, "delete_removed_automation_jobs", slow_cleanup)
    heartbeat = asyncio.create_task(scheduler._check_due_jobs(scheduler._capture_config()))
    try:
        await asyncio.wait_for(started, timeout=1)
        assert time.monotonic() - blocked_at[0] < 0.2
        assert not heartbeat.done()
    finally:
        release.set()
    await asyncio.wait_for(heartbeat, timeout=1)


@pytest.mark.asyncio
async def test_bookkeeping_failure_rolls_back_pending_run_on_repeated_heartbeats(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed schedule advance never leaves or accumulates pending rows."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Bookkeeping failure",
        schedule_type="cron",
        action_type="handler",
        action_config={"handler": "test"},
        cron_expr="0 * * * *",
    )
    due_at = datetime.now(UTC) - timedelta(minutes=5)
    cron_storage.update_job(job.id, next_run_at=due_at)
    claim_due_job = MagicMock(side_effect=RuntimeError("bookkeeping unavailable"))
    monkeypatch.setattr(cron_storage, "claim_due_job", claim_due_job)

    await scheduler._check_due_jobs(scheduler._capture_config())
    await scheduler._check_due_jobs(scheduler._capture_config())

    persisted_job = cron_storage.get_job(job.id)
    assert claim_due_job.call_count == 2
    assert persisted_job is not None
    assert persisted_job.next_run_at == due_at
    assert cron_storage.list_runs(job.id, limit=10) == []
    assert cron_storage.count_running(scheduler._machine_id) == 0
    mock_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_due_one_shot_dispatches_once_and_is_disabled(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """A consumed one-shot clears its schedule without violating job invariants."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    future = datetime.now(UTC) + timedelta(hours=1)
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="One shot",
        schedule_type="once",
        action_type="handler",
        action_config={"handler": "test"},
        run_at=future.isoformat(),
    )
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    cron_storage.db.execute(
        "UPDATE cron_jobs SET run_at = %s, next_run_at = %s WHERE id = %s",
        (due_at, due_at, job.id),
    )

    await scheduler._check_due_jobs(scheduler._capture_config())
    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count == 1,
        description="one-shot cron dispatch",
    )
    await scheduler._check_due_jobs(scheduler._capture_config())

    persisted_job = cron_storage.get_job(job.id)
    runs = cron_storage.list_runs(job.id, limit=10)
    assert persisted_job is not None
    assert persisted_job.enabled is False
    assert persisted_job.next_run_at is None
    assert len(runs) == 1
    assert runs[0].status == "completed"
    mock_executor.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_respects_max_concurrent(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
) -> None:
    """Scheduler respects max_concurrent_jobs limit."""
    config = CronConfig(check_interval_seconds=60, max_concurrent_jobs=1)
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )

    # Create a running run to fill the slot
    job1 = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Running",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    run = cron_storage.create_run(job1.id)
    cron_storage.update_run(run.id, status="running")
    # Simulate the live execution task that owns this run
    scheduler._active_run_ids.add(run.id)

    # Create a due job
    job2 = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Waiting",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    cron_storage.update_job(job2.id, next_run_at=past)

    await scheduler._check_due_jobs(scheduler._capture_config())
    await drain_asyncio_tasks()

    # Should not have dispatched because max concurrent reached
    mock_executor.execute.assert_not_called()
    assert mock_executor.execute.call_count == 0
    assert not mock_executor.execute.called


@pytest.mark.asyncio
async def test_skips_job_with_active_run_but_dispatches_other_due_job(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
) -> None:
    """A long-running job cannot overlap itself, but other jobs can use free slots."""
    config = CronConfig(check_interval_seconds=60, max_concurrent_jobs=2)
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    active_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Active",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    active_run = cron_storage.create_run(active_job.id)
    cron_storage.update_run(active_run.id, status="running")
    # Simulate the live execution task that owns this run
    scheduler._active_run_ids.add(active_run.id)
    cron_storage.update_job(active_job.id, next_run_at=past)
    waiting_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Waiting",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    cron_storage.update_job(waiting_job.id, next_run_at=past)

    await scheduler._check_due_jobs(scheduler._capture_config())
    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 1,
        description="dispatch non-overlapping cron job",
    )

    mock_executor.execute.assert_called_once()
    assert mock_executor.execute.await_args.args[0].id == waiting_job.id
    assert len(cron_storage.list_runs(active_job.id)) == 1


@pytest.mark.asyncio
async def test_due_jobs_skip_legacy_automation_rows_before_dispatch(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """Removed dispatcher cron rows are skipped instead of dispatched."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    names = ["other-job", "gobby:dispatcher", "gobby:pipeline-heartbeat"]
    for name in names:
        job = cron_storage.create_job(
            project_id=PROJECT_ID,
            name=name,
            schedule_type="interval",
            action_type="shell",
            action_config={"command": "echo"},
            interval_seconds=60,
        )
        cron_storage.update_job(job.id, next_run_at=past)

    await scheduler._check_due_jobs(scheduler._capture_config())
    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 1,
        description="non-legacy cron dispatch",
    )

    dispatched = [call.args[0].name for call in mock_executor.execute.await_args_list]
    assert dispatched == ["other-job"]
    assert cron_storage.get_job_by_name("gobby:dispatcher") is not None
    assert cron_storage.get_job_by_name("gobby:pipeline-heartbeat") is not None


@pytest.mark.asyncio
async def test_due_jobs_skip_removed_automation_jobs_returned_after_cleanup(
    config: CronConfig,
) -> None:
    removed_job = CronJob(
        id="cj-removed",
        project_id=PROJECT_ID,
        name="gobby:dispatcher",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "dispatch.tick"},
        created_at="2026-02-10T00:00:00+00:00",
        updated_at="2026-02-10T00:00:00+00:00",
        interval_seconds=60,
        next_run_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    storage = MagicMock()
    storage.delete_removed_automation_jobs.return_value = 0
    storage.get_due_jobs.return_value = [removed_job]
    storage.count_running.return_value = 0
    storage.delete_job.return_value = True
    executor = MagicMock()
    executor.execute = AsyncMock()
    scheduler = CronScheduler(
        storage=storage, executor=executor, capture_bundle=static_cron_capture(config)
    )

    await scheduler._check_due_jobs(scheduler._capture_config())

    storage.delete_job.assert_not_called()
    storage.create_run.assert_not_called()
    executor.execute.assert_not_called()
    assert scheduler._active_tasks == set()
    assert scheduler._running is False


@pytest.mark.asyncio
async def test_stale_tracked_run_is_excluded_from_age_sweep(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
) -> None:
    """The age sweep leaves locally tracked runs to their owning task."""
    config = CronConfig(
        check_interval_seconds=60,
        max_concurrent_jobs=1,
        running_timeout_seconds=60,
        stale_run_grace_seconds=0,
    )
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    stale_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="stale",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    stale_run = cron_storage.create_run(stale_job.id)
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    cron_storage.update_run(stale_run.id, status="running", started_at=old)
    # Simulate a run that is still tracked locally but has exceeded its deadline.
    scheduler._active_run_ids.add(stale_run.id)
    due_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="waiting",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    cron_storage.update_job(due_job.id, next_run_at=old)

    await scheduler._check_due_jobs(scheduler._capture_config())

    refreshed_stale_run = cron_storage.get_run(stale_run.id)
    assert refreshed_stale_run is not None
    assert refreshed_stale_run.status == "running"
    assert refreshed_stale_run.error is None
    assert stale_run.id in scheduler._active_run_ids
    mock_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("orphan_status", ["pending", "running"])
async def test_orphaned_active_run_is_swept_and_job_redispatched(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
    orphan_status: str,
) -> None:
    """An active row with no live task is failed at dispatch time and unblocks its job."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="wedged",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    orphan_run = cron_storage.create_run(job.id, scheduler_owner=scheduler._scheduler_owner)
    assert orphan_run is not None
    if orphan_status == "running":
        cron_storage.update_run(orphan_run.id, status="running", started_at=past)
    cron_storage.update_job(job.id, next_run_at=past)

    await scheduler._check_due_jobs(scheduler._capture_config())
    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 1,
        description="re-dispatch after orphan sweep",
    )

    swept_run = cron_storage.get_run(orphan_run.id)
    assert swept_run is not None
    assert swept_run.status == "failed"
    assert swept_run.error is not None
    assert "no live scheduler task" in swept_run.error
    mock_executor.execute.assert_awaited_once()
    assert mock_executor.execute.await_args.args[0].id == job.id
    assert len(cron_storage.list_runs(job.id, limit=10)) == 2


@pytest.mark.asyncio
async def test_orphaned_active_run_owned_by_other_scheduler_is_not_swept(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """A scheduler only sweeps active rows that it owns."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="foreign-wedged",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    orphan_run = cron_storage.create_run(job.id, scheduler_owner="other-scheduler")
    assert orphan_run is not None
    cron_storage.update_run(orphan_run.id, status="running", started_at=past)
    cron_storage.update_job(job.id, next_run_at=past)

    await scheduler._check_due_jobs(scheduler._capture_config())
    await drain_asyncio_tasks()

    mock_executor.execute.assert_not_awaited()
    preserved_run = cron_storage.get_run(orphan_run.id)
    assert preserved_run is not None
    assert preserved_run.status == "running"
    assert len(cron_storage.list_runs(job.id, limit=10)) == 1


@pytest.mark.asyncio
async def test_due_job_with_live_run_redispatches_after_completion(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """A due job skips while its run is in flight and dispatches once it completes."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="long-runner",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    in_flight = cron_storage.create_run(job.id)
    assert in_flight is not None
    cron_storage.update_run(in_flight.id, status="running", started_at=past)
    scheduler._active_run_ids.add(in_flight.id)
    cron_storage.update_job(job.id, next_run_at=past)

    await scheduler._check_due_jobs(scheduler._capture_config())
    await drain_asyncio_tasks()

    mock_executor.execute.assert_not_awaited()
    assert len(cron_storage.list_runs(job.id, limit=10)) == 1

    # The in-flight run completes and its execution task untracks itself
    now = datetime.now(UTC).isoformat()
    cron_storage.update_run(in_flight.id, status="completed", completed_at=now)
    scheduler._active_run_ids.discard(in_flight.id)

    await scheduler._check_due_jobs(scheduler._capture_config())
    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 1,
        description="re-dispatch after run completion",
    )

    mock_executor.execute.assert_awaited_once()
    assert mock_executor.execute.await_args.args[0].id == job.id
    assert len(cron_storage.list_runs(job.id, limit=10)) == 2


@pytest.mark.asyncio
async def test_run_now_sweeps_orphaned_run_and_proceeds(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """A manual trigger is not blocked by an orphaned active row."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="manual-wedged",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    orphan_run = cron_storage.create_run(job.id, scheduler_owner=scheduler._scheduler_owner)
    assert orphan_run is not None
    cron_storage.update_run(orphan_run.id, status="running")

    result = await scheduler.run_now(job.id)
    await drain_asyncio_tasks()

    assert result is not None
    assert result.id != orphan_run.id
    swept_run = cron_storage.get_run(orphan_run.id)
    assert swept_run is not None
    assert swept_run.status == "failed"
    assert len(cron_storage.list_runs(job.id, limit=10)) == 2


@pytest.mark.asyncio
async def test_execute_and_update_fails_run_when_executor_raises(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """An executor crash terminalizes the run row instead of wedging the job."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="crashing",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    run = cron_storage.create_run(job.id)
    assert run is not None
    mock_executor.execute = AsyncMock(side_effect=RuntimeError("executor exploded"))

    await scheduler._execute_and_update(job, run, scheduler._capture_config())

    failed_run = cron_storage.get_run(run.id)
    assert failed_run is not None
    assert failed_run.status == "failed"
    assert failed_run.error is not None
    assert "Scheduler failed to finalize run" in failed_run.error


@pytest.mark.asyncio
async def test_backoff_on_consecutive_failures(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """Jobs with consecutive failures are skipped during backoff period."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )

    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Failing",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    # Set it as having failed recently with 2 consecutive failures
    now = datetime.now(UTC).isoformat()
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    cron_storage.update_job(
        job.id,
        next_run_at=past,
        last_run_at=now,  # Last run was just now
        consecutive_failures=2,  # 2nd failure -> 60s backoff
    )

    await scheduler._check_due_jobs(scheduler._capture_config())
    await drain_asyncio_tasks()

    # Should be skipped due to backoff
    mock_executor.execute.assert_not_called()
    assert mock_executor.execute.call_count == 0
    assert not mock_executor.execute.called


def test_get_backoff_seconds(scheduler: CronScheduler) -> None:
    """Backoff delays follow config pattern."""
    # Default delays: [30, 60, 300, 900, 3600]
    assert scheduler._get_backoff_seconds(1, scheduler._capture_config()) == 30
    assert scheduler._get_backoff_seconds(2, scheduler._capture_config()) == 60
    assert scheduler._get_backoff_seconds(3, scheduler._capture_config()) == 300
    assert scheduler._get_backoff_seconds(5, scheduler._capture_config()) == 3600
    assert (
        scheduler._get_backoff_seconds(10, scheduler._capture_config()) == 3600
    )  # Capped at last value


@pytest.mark.asyncio
async def test_run_now(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """run_now triggers immediate execution."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )

    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Manual",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )

    run = await scheduler.run_now(job.id)
    assert run is not None
    assert run.cron_job_id == job.id

    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 1,
        description="manual cron execution",
    )
    mock_executor.execute.assert_called_once()


@pytest.mark.asyncio
async def test_run_now_reaches_terminal_state(
    cron_storage: CronJobStorage,
    config: CronConfig,
) -> None:
    """Manual runs move from pending to running to a terminal state."""
    executor = CronExecutor(storage=cron_storage)
    scheduler = CronScheduler(
        storage=cron_storage, executor=executor, capture_bundle=static_cron_capture(config)
    )
    seen_statuses: list[str] = []

    async def handler(job: Any) -> str:
        runs = cron_storage.list_runs(job.id, limit=1)
        seen_statuses.append(runs[0].status)
        return "manual done"

    executor.register_handler("manual.handler", handler)
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Manual Terminal",
        schedule_type="cron",
        action_type="handler",
        action_config={"handler": "manual.handler"},
        cron_expr="0 * * * *",
    )

    run = await scheduler.run_now(job.id)
    assert run is not None
    assert run.status == "pending"

    if scheduler._active_tasks:
        await asyncio.gather(*list(scheduler._active_tasks), return_exceptions=True)

    refreshed_run = cron_storage.get_run(run.id)
    assert seen_statuses == ["running"]
    assert refreshed_run is not None
    assert refreshed_run.status == "completed"
    assert refreshed_run.output == "manual done"


@pytest.mark.asyncio
async def test_run_now_returns_none_when_job_already_running(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
) -> None:
    """Manual runs do not create a row when the same job is already running."""
    config = CronConfig(check_interval_seconds=60, max_concurrent_jobs=1)
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Manual Active",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    active_run = cron_storage.create_run(job.id)
    assert active_run is not None
    cron_storage.update_run(active_run.id, status="running")
    # Simulate the live execution task that owns this run
    scheduler._active_run_ids.add(active_run.id)

    result = await scheduler.run_now(job.id)

    assert result is None
    assert len(cron_storage.list_runs(job.id, limit=10)) == 1
    mock_executor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_run_now_racing_heartbeat_admits_exactly_one_run(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manual and scheduled paths share one atomic per-job admission guard."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Manual heartbeat race",
        schedule_type="cron",
        action_type="handler",
        action_config={"handler": "test"},
        cron_expr="0 * * * *",
    )
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    cron_storage.update_job(job.id, next_run_at=due_at)

    admission_barrier = threading.Barrier(2)
    release_execution = asyncio.Event()
    create_scheduled_run = scheduler._create_scheduled_run
    create_manual_run = cron_storage.create_run_if_admitted

    async def hold_admitted_run(_job: CronJob, run: CronRun) -> CronRun:
        await release_execution.wait()
        updated = cron_storage.update_run(
            run.id,
            status="completed",
            completed_at=datetime.now(UTC),
        )
        return updated or run

    def racing_scheduled_create(job: CronJob, current_config: CronConfig) -> CronRun | None:
        admission_barrier.wait(timeout=2)
        return create_scheduled_run(job, current_config)

    def racing_manual_create(
        cron_job_id: str,
        *,
        machine_id: str,
        max_concurrent_jobs: int,
        scheduler_owner: str | None = None,
    ) -> tuple[CronRun | None, int, bool]:
        admission_barrier.wait(timeout=2)
        return create_manual_run(
            cron_job_id,
            machine_id=machine_id,
            max_concurrent_jobs=max_concurrent_jobs,
            scheduler_owner=scheduler_owner,
        )

    monkeypatch.setattr(scheduler, "_create_scheduled_run", racing_scheduled_create)
    monkeypatch.setattr(cron_storage, "create_run_if_admitted", racing_manual_create)
    mock_executor.execute.side_effect = hold_admitted_run

    try:
        _, manual_run = await asyncio.gather(
            scheduler._check_due_jobs(scheduler._capture_config()),
            scheduler.run_now(job.id),
        )
        await wait_for_async_condition(
            lambda: mock_executor.execute.await_count == 1,
            description="single cron execution after admission race",
        )

        runs = cron_storage.list_runs(job.id, limit=10)
        assert len(runs) == 1
        if manual_run is not None:
            assert manual_run.id == runs[0].id
        mock_executor.execute.assert_awaited_once()
    finally:
        release_execution.set()
        await asyncio.gather(*scheduler._active_tasks)


@pytest.mark.asyncio
async def test_run_now_racing_heartbeat_respects_machine_capacity(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual and scheduled admission share this machine's capacity guard."""
    scheduler = CronScheduler(
        storage=cron_storage,
        executor=mock_executor,
        capture_bundle=static_cron_capture(
            CronConfig(check_interval_seconds=60, max_concurrent_jobs=1)
        ),
    )
    scheduled_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Scheduled capacity race",
        schedule_type="cron",
        action_type="handler",
        action_config={"handler": "test"},
        cron_expr="0 * * * *",
    )
    manual_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Manual capacity race",
        schedule_type="cron",
        action_type="handler",
        action_config={"handler": "test"},
        cron_expr="0 * * * *",
    )
    cron_storage.update_job(
        scheduled_job.id,
        next_run_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    scheduled_admission_ready = threading.Event()
    release_scheduled_admission = threading.Event()
    release_execution = asyncio.Event()
    create_scheduled_run = scheduler._create_scheduled_run

    def delayed_scheduled_create(job: CronJob, current_config: CronConfig) -> CronRun | None:
        scheduled_admission_ready.set()
        if not release_scheduled_admission.wait(timeout=2):
            raise TimeoutError("manual admission did not complete")
        return create_scheduled_run(job, current_config)

    async def hold_admitted_run(_job: CronJob, run: CronRun) -> CronRun:
        await release_execution.wait()
        updated = cron_storage.update_run(
            run.id,
            status="completed",
            completed_at=datetime.now(UTC),
        )
        return updated or run

    monkeypatch.setattr(scheduler, "_create_scheduled_run", delayed_scheduled_create)
    mock_executor.execute.side_effect = hold_admitted_run

    heartbeat = asyncio.create_task(scheduler._check_due_jobs(scheduler._capture_config()))
    try:
        ready = await asyncio.to_thread(scheduled_admission_ready.wait, 2)
        assert ready, "heartbeat did not reach scheduled admission"

        manual_run = await scheduler.run_now(manual_job.id)
        assert manual_run is not None
        release_scheduled_admission.set()
        await heartbeat
        await wait_for_async_condition(
            lambda: mock_executor.execute.await_count == 1,
            description="single cron execution at global capacity",
        )

        runs = cron_storage.list_runs(scheduled_job.id, limit=10) + cron_storage.list_runs(
            manual_job.id,
            limit=10,
        )
        assert len(runs) == 1
        assert runs[0].id == manual_run.id
        assert cron_storage.count_running(manual_run.machine_id) == 1
        mock_executor.execute.assert_awaited_once()
    finally:
        release_scheduled_admission.set()
        release_execution.set()
        await heartbeat
        await asyncio.gather(*scheduler._active_tasks)


@pytest.mark.asyncio
async def test_run_now_rejects_when_max_concurrency_full(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
) -> None:
    """Manual runs do not create a row when global concurrency is full."""
    config = CronConfig(check_interval_seconds=60, max_concurrent_jobs=1)
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    active_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Active Other",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    idle_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Manual Idle",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    active_run = cron_storage.create_run(active_job.id)
    cron_storage.update_run(active_run.id, status="running")
    # Simulate the live execution task that owns this run
    scheduler._active_run_ids.add(active_run.id)

    with pytest.raises(CronRunRejected) as exc_info:
        await scheduler.run_now(idle_job.id)

    assert exc_info.value.code == "cron_max_concurrent_jobs"
    assert cron_storage.list_runs(idle_job.id, limit=10) == []
    mock_executor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_run_now_executes_in_empty_session_context(
    cron_storage: CronJobStorage,
    config: CronConfig,
    temp_db: HubDatabase,
    sample_project,
) -> None:
    """Manual cron ticks must not inherit the caller session context."""
    from gobby.mcp_proxy.tools.cron import create_cron_registry
    from gobby.storage.sessions import SessionManager
    from gobby.utils.project_context import get_project_context
    from gobby.utils.session_context import get_current_session_id, session_context_for_test
    from gobby.workflows.state_manager import SessionVariableManager
    from gobby.workflows.step_instances import AgentStepInstanceManager

    executor = CronExecutor(storage=cron_storage)
    scheduler = CronScheduler(
        storage=cron_storage, executor=executor, capture_bundle=static_cron_capture(config)
    )
    seen: dict[str, object] = {}

    async def dispatch_tick_handler(job) -> str:
        current_session = get_current_session_id()
        seen["session_id"] = current_session
        project_ctx = get_project_context()
        seen["project_id"] = project_ctx.get("id") if project_ctx else None
        if current_session:
            SessionVariableManager(temp_db).set_variable(
                current_session,
                "_agent_type",
                "backend-developer",
            )
        return "tick"

    executor.register_handler("dispatch.tick", dispatch_tick_handler)
    job = cron_storage.create_job(
        project_id=sample_project["id"],
        name="gobby:dispatcher",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "dispatch.tick"},
        interval_seconds=60,
    )
    caller = SessionManager(temp_db).register(
        external_id="cron-caller",
        machine_id="21000000-0000-4000-8000-000000000003",
        source="codex",
        project_id=sample_project["id"],
    )
    registry = create_cron_registry(cron_storage, scheduler)
    run_cron_job = registry.get_tool("run_cron_job")

    with session_context_for_test(caller.id):
        result = await run_cron_job(job.id)

    if scheduler._active_tasks:
        await asyncio.gather(*list(scheduler._active_tasks), return_exceptions=True)

    assert result["success"] is True
    assert seen == {"session_id": None, "project_id": sample_project["id"]}
    caller_vars = SessionVariableManager(temp_db).get_variables(caller.id)
    assert "_agent_type" not in caller_vars
    assert AgentStepInstanceManager(temp_db).get_for_session(caller.id) is None


@pytest.mark.asyncio
async def test_run_now_nonexistent_job(scheduler: CronScheduler) -> None:
    """run_now returns None for non-existent job."""
    result = await scheduler.run_now("00000000-0000-0000-0000-0000000000ff")
    assert result is None


@pytest.mark.asyncio
async def test_execute_and_update_success(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """_execute_and_update resets failure counter on success."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )

    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Success",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    cron_storage.update_job(job.id, consecutive_failures=3)

    run = cron_storage.create_run(job.id)
    await scheduler._execute_and_update(job, run, scheduler._capture_config())

    updated_job = cron_storage.get_job(job.id)
    assert updated_job is not None
    assert updated_job.consecutive_failures == 0
    assert updated_job.last_status == "completed"


async def test_execute_and_update_failure_logs_selected_backoff(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Failing",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "false"},
        cron_expr="0 * * * *",
    )
    run = cron_storage.create_run(job.id)
    assert run is not None
    cron_storage.update_run(run.id, status="failed", error="command failed")
    failed_run = cron_storage.get_run(run.id)
    assert failed_run is not None
    mock_executor.execute = AsyncMock(return_value=failed_run)
    caplog.set_level("WARNING", logger="gobby.scheduler.scheduler")

    await scheduler._execute_and_update(job, run, scheduler._capture_config())

    assert (
        f"Cron job {job.id} ({job.name}) failed; applying 30s backoff after 1 consecutive failure"
        in caplog.text
    )


@pytest.mark.asyncio
async def test_execute_and_update_dispatched_resets_failure_counter(
    cron_storage: CronJobStorage,
    config: CronConfig,
) -> None:
    """Dispatched cron runs are terminal non-failures for job bookkeeping."""
    executor = CronExecutor(storage=cron_storage)
    scheduler = CronScheduler(
        storage=cron_storage, executor=executor, capture_bundle=static_cron_capture(config)
    )
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Dispatched",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    cron_storage.update_job(job.id, consecutive_failures=2)
    run = cron_storage.create_run(job.id)
    assert run is not None
    cron_storage.update_run(run.id, status="dispatched")
    executor.execute = AsyncMock(return_value=cron_storage.get_run(run.id))

    await scheduler._execute_and_update(job, run, scheduler._capture_config())

    updated_job = cron_storage.get_job(job.id)
    assert updated_job is not None
    assert updated_job.consecutive_failures == 0
    assert updated_job.last_status == "dispatched"


@pytest.mark.asyncio
async def test_execute_and_update_skipped_resets_failure_counter(
    cron_storage: CronJobStorage,
    config: CronConfig,
) -> None:
    """Skipped cron runs do not increment backoff."""
    executor = CronExecutor(storage=cron_storage)
    scheduler = CronScheduler(
        storage=cron_storage, executor=executor, capture_bundle=static_cron_capture(config)
    )
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Skipped",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    cron_storage.update_job(job.id, consecutive_failures=2)
    run = cron_storage.create_run(job.id)
    assert run is not None
    cron_storage.update_run(run.id, status="skipped")
    executor.execute = AsyncMock(return_value=cron_storage.get_run(run.id))

    await scheduler._execute_and_update(job, run, scheduler._capture_config())

    updated_job = cron_storage.get_job(job.id)
    assert updated_job is not None
    assert updated_job.consecutive_failures == 0
    assert updated_job.last_status == "skipped"


@pytest.mark.asyncio
async def test_on_run_complete_callback_fires(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """on_run_complete callback fires after job execution with correct args."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )

    callback = AsyncMock()
    scheduler.on_run_complete = callback

    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Callback Test",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )

    run = cron_storage.create_run(job.id)
    await scheduler._execute_and_update(job, run, scheduler._capture_config())

    callback.assert_called_once()
    call_args = callback.call_args[0]
    assert call_args[0].id == job.id  # CronJob
    assert call_args[1].status == "completed"  # CronRun


@pytest.mark.asyncio
async def test_on_run_complete_callback_error_does_not_propagate(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """on_run_complete callback errors are swallowed (best-effort)."""
    scheduler = CronScheduler(
        storage=cron_storage, executor=mock_executor, capture_bundle=static_cron_capture(config)
    )

    callback = AsyncMock(side_effect=RuntimeError("callback exploded"))
    scheduler.on_run_complete = callback

    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Callback Error Test",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )

    run = cron_storage.create_run(job.id)
    # Should not raise despite callback error
    await scheduler._execute_and_update(job, run, scheduler._capture_config())

    callback.assert_called_once()
    # Job should still be updated correctly
    updated_job = cron_storage.get_job(job.id)
    assert updated_job is not None
    assert updated_job.last_status == "completed"


@pytest.mark.asyncio
async def test_on_run_complete_not_called_without_result(
    cron_storage: CronJobStorage,
    config: CronConfig,
) -> None:
    """on_run_complete not called when _execute_and_update gets no run."""
    executor = CronExecutor(storage=cron_storage)
    scheduler = CronScheduler(
        storage=cron_storage, executor=executor, capture_bundle=static_cron_capture(config)
    )

    callback = AsyncMock()
    scheduler.on_run_complete = callback

    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="No Run Test",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )

    # Pass None run — should bail early without calling callback
    await scheduler._execute_and_update(job, None, scheduler._capture_config())
    callback.assert_not_called()
    assert callback.call_count == 0
    assert not callback.called


def test_stale_sweep_excludes_runs_tracked_by_local_scheduler() -> None:
    storage = MagicMock()
    storage.fail_stale_running_runs.return_value = 0
    config = CronConfig(running_timeout_seconds=60, stale_run_grace_seconds=0)
    scheduler = CronScheduler(
        storage=storage,
        executor=MagicMock(),
        capture_bundle=static_cron_capture(config),
    )
    scheduler._active_run_ids.update({"run-a", "run-b"})

    swept = scheduler._sweep_stale_running_runs(scheduler._capture_config())

    assert swept == 0
    storage.fail_stale_running_runs.assert_called_once_with(
        60,
        machine_id=scheduler._machine_id,
        exclude_run_ids={"run-a", "run-b"},
    )
