"""Integration tests for gobby-cron with real DB.

Validates the full cron lifecycle: scheduling, execution, backoff, cleanup.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from gobby.scheduler.executor import CronExecutor
from gobby.scheduler.scheduler import CronScheduler
from gobby.storage.cron import CronJobStorage, compute_next_run
from gobby.storage.cron_models import CronJob
from tests._timing import wait_for_async_condition

if TYPE_CHECKING:
    from gobby.config.cron import CronConfig
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def cron_storage(temp_db: HubDatabase) -> CronJobStorage:
    return CronJobStorage(temp_db)


@pytest.fixture
def executor(cron_storage: CronJobStorage) -> CronExecutor:
    return CronExecutor(storage=cron_storage)


@pytest.fixture
def cron_config() -> CronConfig:
    from gobby.config.cron import CronConfig

    return CronConfig(
        enabled=True,
        check_interval_seconds=60,
        max_concurrent_jobs=5,
        cleanup_after_days=7,
    )


@pytest.fixture
def scheduler(
    cron_storage: CronJobStorage,
    executor: CronExecutor,
    cron_config: CronConfig,
) -> CronScheduler:
    return CronScheduler(
        storage=cron_storage,
        executor=executor,
        config=cron_config,
    )


# --- Shell job via run_now ---


@pytest.mark.asyncio
async def test_shell_job_run_now(
    cron_storage: CronJobStorage,
    scheduler: CronScheduler,
) -> None:
    """Shell job executed via run_now produces completed CronRun with output."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="echo-test",
        schedule_type="interval",
        interval_seconds=3600,
        action_type="shell",
        action_config={"command": "echo", "args": ["integration-test"]},
    )

    run = await scheduler.run_now(job.id)
    assert run is not None
    assert run.status == "pending"  # Initially pending, runs async

    await wait_for_async_condition(
        lambda: (cron_storage.get_run(run.id) or run).status == "completed",
        description="shell cron completion",
    )

    # Fetch fresh run
    final = cron_storage.get_run(run.id)
    assert final is not None
    assert final.status == "completed"
    assert "integration-test" in (final.output or "")


# --- Interval job scheduling ---


def test_interval_job_due_after_time(cron_storage: CronJobStorage) -> None:
    """Interval job becomes due after interval_seconds elapses."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="interval-test",
        schedule_type="interval",
        interval_seconds=60,
        action_type="shell",
        action_config={"command": "echo"},
    )

    # Should not be due immediately (next_run is ~30s from now)
    due = cron_storage.get_due_jobs()
    due_ids = [j.id for j in due]
    assert job.id not in due_ids

    # Backdate next_run_at to make it due
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    cron_storage.update_job(job.id, next_run_at=past)

    due = cron_storage.get_due_jobs()
    due_ids = [j.id for j in due]
    assert job.id in due_ids


# --- Handler action type dispatch ---


@pytest.mark.asyncio
async def test_handler_dispatch_via_run_now(
    cron_storage: CronJobStorage,
    executor: CronExecutor,
    scheduler: CronScheduler,
) -> None:
    """Handler action type dispatches to registered callback via run_now."""
    call_log: list[str] = []

    async def tick_handler(job: CronJob) -> str:
        call_log.append(job.name)
        return "tick completed"

    executor.register_handler("tick", tick_handler)

    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="handler-test",
        schedule_type="interval",
        interval_seconds=3600,
        action_type="handler",
        action_config={"handler": "tick"},
    )

    run = await scheduler.run_now(job.id)
    assert run is not None

    await wait_for_async_condition(lambda: call_log, description="handler cron dispatch")

    assert call_log == ["handler-test"]
    final = cron_storage.get_run(run.id)
    assert final is not None
    assert final.status == "completed"
    assert "tick completed" in (final.output or "")


# --- Backoff on consecutive failures ---


@pytest.mark.asyncio
async def test_consecutive_failure_backoff(
    cron_storage: CronJobStorage,
    scheduler: CronScheduler,
) -> None:
    """Failed jobs increment consecutive_failures counter."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="fail-test",
        schedule_type="interval",
        interval_seconds=60,
        action_type="shell",
        action_config={"command": "false"},  # always exits 1
    )

    # Run it
    run = await scheduler.run_now(job.id)
    assert run is not None

    await wait_for_async_condition(
        lambda: (cron_storage.get_job(job.id) or job).consecutive_failures == 1,
        description="first failed cron run",
    )

    # Check failure counter incremented
    updated_job = cron_storage.get_job(job.id)
    assert updated_job is not None
    assert updated_job.consecutive_failures == 1
    assert updated_job.last_status == "failed"

    # Run again
    run2 = await scheduler.run_now(job.id)
    assert run2 is not None
    await wait_for_async_condition(
        lambda: (cron_storage.get_job(job.id) or job).consecutive_failures == 2,
        description="second failed cron run",
    )

    updated_job2 = cron_storage.get_job(job.id)
    assert updated_job2 is not None
    assert updated_job2.consecutive_failures == 2


# --- Old run cleanup ---


def test_cleanup_old_runs(cron_storage: CronJobStorage) -> None:
    """cleanup_old_runs deletes runs older than the threshold."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="cleanup-test",
        schedule_type="interval",
        interval_seconds=3600,
        action_type="shell",
        action_config={"command": "echo"},
    )

    # Create a run and backdate it
    run = cron_storage.create_run(job.id)
    old_time = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    cron_storage.update_run(run.id, status="completed", completed_at=old_time)
    # Backdate created_at directly
    cron_storage.db.execute(
        "UPDATE cron_runs SET created_at = ? WHERE id = ?",
        (old_time, run.id),
    )

    # Create a recent run
    recent_run = cron_storage.create_run(job.id)
    cron_storage.update_run(recent_run.id, status="completed")

    # Cleanup runs older than 7 days
    deleted = cron_storage.cleanup_old_runs(days=7)
    assert deleted == 1

    # Old run gone, recent run still there
    assert cron_storage.get_run(run.id) is None
    assert cron_storage.get_run(recent_run.id) is not None


# --- get_job_by_name ---


def test_get_job_by_name(cron_storage: CronJobStorage) -> None:
    """get_job_by_name finds jobs by name and returns None for missing."""
    cron_storage.create_job(
        project_id=PROJECT_ID,
        name="gobby:pipeline-heartbeat",
        schedule_type="interval",
        interval_seconds=60,
        action_type="handler",
        action_config={"handler": "pipeline_heartbeat"},
    )

    found = cron_storage.get_job_by_name("gobby:pipeline-heartbeat")
    assert found is not None
    assert found.name == "gobby:pipeline-heartbeat"
    assert found.action_type == "handler"

    missing = cron_storage.get_job_by_name("nonexistent")
    assert missing is None


# --- compute_next_run ---


def test_compute_next_run_disabled_job(cron_storage: CronJobStorage) -> None:
    """Disabled jobs have no next run."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="disabled-test",
        schedule_type="interval",
        interval_seconds=60,
        action_type="shell",
        action_config={"command": "echo"},
        enabled=False,
    )

    assert compute_next_run(job) is None
