"""Tests for CronScheduler background task logic."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.cron import CronConfig
from gobby.scheduler.executor import CronExecutor
from gobby.scheduler.scheduler import CronScheduler
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronRun

if TYPE_CHECKING:
    from gobby.storage.database import LocalDatabase

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def cron_storage(temp_db: LocalDatabase) -> CronJobStorage:
    return CronJobStorage(temp_db)


@pytest.fixture
def mock_executor(cron_storage: CronJobStorage) -> CronExecutor:
    executor = CronExecutor(storage=cron_storage)

    def _complete_run(job: Any, run: CronRun) -> CronRun:
        """Helper to mark a run as completed."""
        now = datetime.now(UTC).isoformat()
        updated = cron_storage.update_run(run.id, status="completed", completed_at=now)
        return updated or run

    executor.execute = AsyncMock(side_effect=_complete_run)  # type: ignore[method-assign]
    return executor


@pytest.fixture
def config() -> CronConfig:
    return CronConfig(check_interval_seconds=10, max_concurrent_jobs=5)


@pytest.fixture
def scheduler(
    cron_storage: CronJobStorage, mock_executor: CronExecutor, config: CronConfig
) -> CronScheduler:
    return CronScheduler(storage=cron_storage, executor=mock_executor, config=config)


@pytest.mark.asyncio
async def test_start_creates_tasks(scheduler: CronScheduler) -> None:
    """start() creates check and cleanup tasks."""
    await scheduler.start()
    assert scheduler._running is True
    assert scheduler._check_task is not None
    assert scheduler._cleanup_task is not None
    await scheduler.stop()


@pytest.mark.asyncio
async def test_stop_cancels_tasks(scheduler: CronScheduler) -> None:
    """stop() cancels tasks gracefully."""
    await scheduler.start()
    await scheduler.stop()
    assert scheduler._running is False


@pytest.mark.asyncio
async def test_double_start_is_noop(scheduler: CronScheduler) -> None:
    """Calling start() twice doesn't create duplicate tasks."""
    await scheduler.start()
    task1 = scheduler._check_task
    await scheduler.start()  # Should be a no-op
    assert scheduler._check_task is task1
    await scheduler.stop()


@pytest.mark.asyncio
async def test_disabled_scheduler_does_not_start() -> None:
    """Scheduler doesn't start when config.enabled is False."""
    config = CronConfig(enabled=False)
    scheduler = CronScheduler(storage=MagicMock(), executor=MagicMock(), config=config)
    await scheduler.start()
    assert scheduler._running is False
    assert scheduler._check_task is None


@pytest.mark.asyncio
async def test_check_due_jobs_dispatches(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """_check_due_jobs dispatches due jobs to executor."""
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)

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

    await scheduler._check_due_jobs()
    # Give the background task a moment to complete
    await asyncio.sleep(0.1)

    mock_executor.execute.assert_called_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_respects_max_concurrent(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
) -> None:
    """Scheduler respects max_concurrent_jobs limit."""
    config = CronConfig(check_interval_seconds=10, max_concurrent_jobs=1)
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)

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

    await scheduler._check_due_jobs()
    await asyncio.sleep(0.1)

    # Should not have dispatched because max concurrent reached
    mock_executor.execute.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_backoff_on_consecutive_failures(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """Jobs with consecutive failures are skipped during backoff period."""
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)

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

    await scheduler._check_due_jobs()
    await asyncio.sleep(0.1)

    # Should be skipped due to backoff
    mock_executor.execute.assert_not_called()  # type: ignore[attr-defined]


def test_get_backoff_seconds(scheduler: CronScheduler) -> None:
    """Backoff delays follow config pattern."""
    # Default delays: [30, 60, 300, 900, 3600]
    assert scheduler._get_backoff_seconds(1) == 30
    assert scheduler._get_backoff_seconds(2) == 60
    assert scheduler._get_backoff_seconds(3) == 300
    assert scheduler._get_backoff_seconds(5) == 3600
    assert scheduler._get_backoff_seconds(10) == 3600  # Capped at last value


@pytest.mark.asyncio
async def test_run_now(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """run_now triggers immediate execution."""
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)

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

    await asyncio.sleep(0.1)
    mock_executor.execute.assert_called_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_now_nonexistent_job(scheduler: CronScheduler) -> None:
    """run_now returns None for non-existent job."""
    result = await scheduler.run_now("cj-nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_execute_and_update_success(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """_execute_and_update resets failure counter on success."""
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)

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
    await scheduler._execute_and_update(job, run)

    updated_job = cron_storage.get_job(job.id)
    assert updated_job is not None
    assert updated_job.consecutive_failures == 0
    assert updated_job.last_status == "completed"
